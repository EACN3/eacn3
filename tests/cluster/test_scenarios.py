"""Scenario tests: cross-node lifecycle, forwarding, error paths, gossip chains.

Only tests that exercise behavior NOT covered by individual module tests.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn3.network.app import Network
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn3.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network
from eacn3.network.cluster.node import NodeCard, MembershipList
from eacn3.network.cluster.gossip import ClusterGossip
from eacn3.network.cluster.router import ClusterRouter
from eacn3.network.cluster.service import ClusterService
from eacn3.network.cluster.bootstrap import ClusterBootstrap
from eacn3.network.config import ClusterConfig, NetworkConfig


def _mock_httpx():
    """Helper: returns (mock_client_cls, mock_client, mock_response) for patching httpx."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"ok": True}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client, mock_response


@pytest.fixture
async def api():
    """Full API client with Network + Cluster wired up."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    await net.start()
    net.escrow.get_or_create_account("user1", 10_000.0)
    await net.dht.announce("coding", "a1")
    net.reputation._scores["a1"] = 0.8

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


# ── Forwarding via httpx ──────────────────────────────────────────────

class TestForwardingHTTP:
    """Router forward methods build correct HTTP calls."""

    async def test_forward_bid_payload(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        router.set_endpoint("remote", "http://r:8000")

        mock_client, mock_resp = _mock_httpx()
        mock_resp.json.return_value = {"status": "executing", "bid": {"agent_id": "a1"}}

        with patch("eacn3.network.cluster.router.httpx.AsyncClient", return_value=mock_client):
            result = await router.forward_bid("t1", "a1", "srv", 0.9, 80.0)

        assert result["status"] == "executing"
        p = mock_client.post.call_args[1]["json"]
        assert p == {"task_id": "t1", "agent_id": "a1", "server_id": "srv",
                     "confidence": 0.9, "price": 80.0, "from_node": "local"}

    async def test_forward_propagates_http_errors(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        router.set_endpoint("remote", "http://r:8000")

        mock_client, _ = _mock_httpx()
        mock_client.post.side_effect = httpx.ConnectError("refused")

        with patch("eacn3.network.cluster.router.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.ConnectError):
                await router.forward_bid("t1", "a1", None, 0.9, 80.0)


class TestNotifyAndBroadcast:
    """_broadcast_to_nodes skips self, swallows errors, skips missing endpoints."""

    async def test_notify_skips_self_and_missing(self, db):
        router = ClusterRouter(db, "local")
        router.set_endpoint("a", "http://a:8000")

        mock_client, _ = _mock_httpx()
        with patch("eacn3.network.cluster.router.httpx.AsyncClient", return_value=mock_client):
            await router.notify_status("t1", "done", {"local", "a", "missing"})

        assert mock_client.post.call_count == 1
        assert "a:8000" in mock_client.post.call_args[0][0]

    async def test_notify_swallows_individual_failures(self, db):
        router = ClusterRouter(db, "local")
        router.set_endpoint("a", "http://a:8000")
        router.set_endpoint("b", "http://b:8000")

        mock_client, _ = _mock_httpx()
        mock_client.post.side_effect = [httpx.ConnectError("x"), MagicMock()]
        with patch("eacn3.network.cluster.router.httpx.AsyncClient", return_value=mock_client):
            await router.notify_status("t1", "done", {"a", "b"})  # no raise

    async def test_broadcast_task_to_peers(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False
        peer = NodeCard(node_id="p1", endpoint="http://p1:8000", domains=["coding"])
        cs.members.add(peer)
        cs.router.set_endpoint("p1", "http://p1:8000")
        await cs.dht.announce("coding", "p1")

        mock_client, _ = _mock_httpx()
        with patch("eacn3.network.cluster.service.httpx.AsyncClient", return_value=mock_client):
            notified = await cs.broadcast_task({"task_id": "t1", "domains": ["coding"]})

        assert notified == ["p1"]
        assert "origin" in mock_client.post.call_args[1]["json"]

    async def test_broadcast_deduplicates_multi_domain(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False
        multi = NodeCard(node_id="m", endpoint="http://m:8000", domains=["a", "b"])
        cs.members.add(multi)
        cs.router.set_endpoint("m", "http://m:8000")
        await cs.dht.announce("a", "m")
        await cs.dht.announce("b", "m")

        mock_client, _ = _mock_httpx()
        with patch("eacn3.network.cluster.service.httpx.AsyncClient", return_value=mock_client):
            notified = await cs.broadcast_task({"task_id": "t1", "domains": ["a", "b"]})

        assert notified == ["m"]
        assert mock_client.post.call_count == 1


# ── Bootstrap error paths ─────────────────────────────────────────────

class TestBootstrapErrors:
    async def test_all_seeds_unreachable(self):
        local = NodeCard(node_id="n1", endpoint="http://n1:8000")
        bs = ClusterBootstrap(local, MembershipList(), ClusterConfig(seed_nodes=["http://s:8000"]))
        with patch.object(bs, "_contact_seed", side_effect=httpx.ConnectError("x")):
            assert await bs.join_network() == []

    async def test_first_seed_fails_second_succeeds(self):
        local = NodeCard(node_id="n1", endpoint="http://n1:8000")
        bs = ClusterBootstrap(local, MembershipList(),
                              ClusterConfig(seed_nodes=["http://s1:8000", "http://s2:8000"]))
        peer = NodeCard(node_id="p", endpoint="http://p:8000")

        async def contact(ep):
            if "s1" in ep:
                raise httpx.ConnectError("x")
            return [peer]

        with patch.object(bs, "_contact_seed", side_effect=contact):
            result = await bs.join_network()
        assert len(result) == 1

    async def test_start_populates_members(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://l:8000",
                               seed_nodes=["http://s:8000"])
        cs = ClusterService(db, config=config)
        peers = [NodeCard(node_id=f"p{i}", endpoint=f"http://p{i}:8000") for i in range(2)]
        with patch.object(cs.bootstrap, "join_network", return_value=peers):
            await cs.start()
        assert cs.members.count() == 3


# ── API routing: local vs remote ──────────────────────────────────────

class TestAPIRouting:
    async def test_api_bid_forward_failure_returns_502(self, api):
        client, net = api
        net.cluster.router.set_route("rt1", "remote")
        net.cluster.router.set_endpoint("remote", "http://r:8000")

        mock_client, _ = _mock_httpx()
        mock_client.post.side_effect = httpx.ConnectError("x")
        with patch("eacn3.network.cluster.router.httpx.AsyncClient", return_value=mock_client):
            resp = await client.post("/api/tasks/rt1/bid",
                                     json={"agent_id": "a1", "confidence": 0.9, "price": 80.0})
        assert resp.status_code == 502

    async def test_api_bid_no_endpoint_returns_502(self, api):
        client, net = api
        net.cluster.router.set_route("rt1", "remote")  # no endpoint
        resp = await client.post("/api/tasks/rt1/bid",
                                 json={"agent_id": "a1", "confidence": 0.9, "price": 80.0})
        assert resp.status_code == 502

    async def test_broadcast_idempotency(self, api):
        client, net = api
        body = {"task_id": "dup", "origin": "B", "initiator_id": "u1", "domains": ["coding"]}
        r1 = await client.post("/peer/task/broadcast", json=body)
        r2 = await client.post("/peer/task/broadcast", json=body)
        assert r1.status_code == r2.status_code == 200
        assert net.cluster.router.get_route("dup") == "B"

    async def test_join_endpoint_conflict_returns_409(self, api):
        client, _ = api
        card = {"node_id": "c", "endpoint": "http://a:8000", "version": "0.1.0"}
        await client.post("/peer/join", json={"node_card": card})
        card["endpoint"] = "http://b:8000"
        resp = await client.post("/peer/join", json={"node_card": card})
        assert resp.status_code == 409


# ── Cross-node lifecycle ──────────────────────────────────────────────

class TestCrossNodeLifecycle:
    async def test_full_lifecycle(self, api):
        """Create task → peer bid → peer result → close → select."""
        client, net = api

        resp = await client.post("/api/tasks", json={
            "task_id": "x1", "initiator_id": "user1",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 201

        resp = await client.post("/peer/task/bid", json={
            "task_id": "x1", "agent_id": "a1", "confidence": 0.9,
            "price": 80.0, "from_node": "node-B",
        })
        assert resp.json()["status"] == "executing"
        assert "node-B" in net.cluster.router.get_participants("x1")

        resp = await client.post("/peer/task/result", json={
            "task_id": "x1", "agent_id": "a1",
            "content": "done", "from_node": "node-B",
        })
        assert resp.status_code == 200

        await client.post("/api/tasks/x1/close", json={"initiator_id": "user1"})
        resp = await client.post("/api/tasks/x1/select",
                                 json={"initiator_id": "user1", "agent_id": "a1"})
        assert resp.status_code == 200


# ── Gossip chain propagation ─────────────────────────────────────────

class TestGossipChain:
    async def test_three_node_chain_propagation(self, db):
        """A↔B, B↔C, A↔B → A knows C."""
        gossip = ClusterGossip(db, MembershipList())
        await gossip.exchange("a", "b")
        await gossip.exchange("b", "c")
        await gossip.exchange("a", "b")
        assert "c" in await gossip.get_known("a")

    async def test_remove_prevents_resurrection(self, db):
        gossip = ClusterGossip(db, MembershipList())
        await gossip.exchange("a", "b")
        await gossip.exchange("a", "c")
        await gossip.remove_node("b")
        await gossip.exchange("a", "c")
        assert "b" not in await gossip.get_known("a")

    async def test_exchange_self_is_noop(self, db):
        gossip = ClusterGossip(db, MembershipList())
        await gossip.exchange("a", "a")
        assert "a" not in await gossip.get_known("a")


# ── Leave/rejoin consistency ─────────────────────────────────────────

class TestLeaveRejoin:
    async def test_leave_then_rejoin_different_endpoint(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://l:8000")
        cs = ClusterService(db, config=config)
        cs.handle_join(NodeCard(node_id="p", endpoint="http://old:8000"))
        cs.handle_leave("p")
        cs.handle_join(NodeCard(node_id="p", endpoint="http://new:8000"))
        assert cs.members.get("p").endpoint == "http://new:8000"

    async def test_stop_revokes_dht(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://l:8000",
                               seed_nodes=["http://s:8000"])
        cs = ClusterService(db, config=config)
        cs._standalone = False
        await cs.dht.announce("coding", "local")
        with patch.object(cs.bootstrap, "leave_network"):
            await cs.stop()
        assert await cs.dht.lookup("coding") == []
