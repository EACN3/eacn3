"""Tests for multi-step operations, gossip chains, and complex interactions.

Covers: rapid join/leave sequences, gossip propagation chains, multi-domain
discovery, full cross-node task lifecycle simulation, and concurrent-like patterns.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn.network.app import Network
from eacn.network.db import Database
from eacn.network.api.routes import router as net_router, set_network
from eacn.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network
from eacn.network.api.websocket import ws_router
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.discovery import ClusterDiscovery
from eacn.network.cluster.router import ClusterRouter
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.service import ClusterService
from eacn.network.config import ClusterConfig


class TestRapidJoinLeaveSequences:
    async def test_join_leave_join_same_node(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="flaky", endpoint="http://flaky:8000", domains=["coding"])
        cs.handle_join(peer)
        assert cs.members.contains("flaky") is True

        cs.handle_leave("flaky")
        assert cs.members.contains("flaky") is False

        # Rejoin
        peer2 = NodeCard(node_id="flaky", endpoint="http://flaky:8000", domains=["coding"])
        cs.handle_join(peer2)
        assert cs.members.contains("flaky") is True
        assert cs.members.get("flaky").domains == ["coding"]

    async def test_many_joins_then_leaves(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        for i in range(20):
            peer = NodeCard(node_id=f"n{i}", endpoint=f"http://n{i}:8000")
            cs.handle_join(peer)

        assert cs.members.count() == 21  # local + 20

        for i in range(20):
            cs.handle_leave(f"n{i}")

        assert cs.members.count() == 1  # only local
        assert cs.members.contains("local") is True

    async def test_join_with_heartbeat_updates(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["old"])
        cs.handle_join(peer)

        # Multiple heartbeats with domain updates
        cs.handle_heartbeat("peer", ["d1"], "2026-01-01T01:00:00Z")
        assert cs.members.get("peer").domains == ["d1"]

        cs.handle_heartbeat("peer", ["d1", "d2"], "2026-01-01T02:00:00Z")
        assert cs.members.get("peer").domains == ["d1", "d2"]

        cs.handle_heartbeat("peer", ["d3"], "2026-01-01T03:00:00Z")
        assert cs.members.get("peer").domains == ["d3"]
        assert cs.members.get("peer").last_seen == "2026-01-01T03:00:00Z"


class TestGossipPropagationChains:
    async def test_three_node_chain(self, db):
        """A↔B, B↔C, A↔B again → A knows C."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")
        await gossip.exchange("b", "c")
        await gossip.exchange("a", "b")

        a_knows = await gossip.get_known("a")
        assert "b" in a_knows
        assert "c" in a_knows

        c_knows = await gossip.get_known("c")
        assert "b" in c_knows
        # C should know A through B
        await gossip.exchange("b", "c")
        c_knows = await gossip.get_known("c")
        assert "a" in c_knows

    async def test_four_node_full_mesh_gossip(self, db):
        """Exchange all pairs → everyone knows everyone."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        nodes = ["a", "b", "c", "d"]

        # Do pairwise exchanges
        for i, n1 in enumerate(nodes):
            for n2 in nodes[i + 1:]:
                await gossip.exchange(n1, n2)

        # Everyone should know everyone else
        for node in nodes:
            knows = await gossip.get_known(node)
            others = {n for n in nodes if n != node}
            assert knows >= others, f"{node} should know {others}, but knows {knows}"

    async def test_gossip_with_node_removal_midway(self, db):
        """Gossip, remove a node, continue gossiping — removed node shouldn't reappear."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")
        await gossip.exchange("b", "c")
        await gossip.exchange("a", "c")

        # All three know each other now
        await gossip.remove_node("b")

        # Further exchanges between a and c shouldn't resurrect b
        await gossip.exchange("a", "c")

        a_knows = await gossip.get_known("a")
        c_knows = await gossip.get_known("c")
        assert "b" not in a_knows
        assert "b" not in c_knows


class TestMultiDomainDiscovery:
    async def test_discovery_across_multiple_domains(self, db):
        """Task with multiple domains should discover nodes from all domains."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        from eacn.network.cluster.node import NodeCard
        coding_node = NodeCard(node_id="coder", endpoint="http://c:8000", domains=["coding"])
        design_node = NodeCard(node_id="designer", endpoint="http://d:8000", domains=["design"])
        cs.members.add(coding_node)
        cs.members.add(design_node)
        cs.router.set_endpoint("coder", "http://c:8000")
        cs.router.set_endpoint("designer", "http://d:8000")
        await cs.dht.announce("coding", "coder")
        await cs.dht.announce("design", "designer")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1",
                "domains": ["coding", "design"],
            })

        assert set(notified) == {"coder", "designer"}

    async def test_discovery_deduplicates_when_node_handles_multiple_domains(self, db):
        """If a node handles both domains, it should only be notified once."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        from eacn.network.cluster.node import NodeCard
        multi = NodeCard(node_id="multi", endpoint="http://m:8000", domains=["coding", "design"])
        cs.members.add(multi)
        cs.router.set_endpoint("multi", "http://m:8000")
        await cs.dht.announce("coding", "multi")
        await cs.dht.announce("design", "multi")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1",
                "domains": ["coding", "design"],
            })

        assert notified == ["multi"]
        # Only one HTTP call
        assert mock_client.post.call_count == 1


class TestCrossNodeTaskLifecycle:
    """Simulate a full task lifecycle across two nodes using the peer API."""

    @pytest.fixture
    async def two_node_client(self):
        """Two-node simulation: tasks created on node-A, bids from node-B via peer routes."""
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

    async def test_full_cross_node_lifecycle(self, two_node_client):
        """Simulate: Node-B broadcasts task to Node-A. Node-A bids via peer route. Full lifecycle."""
        client, net = two_node_client

        # 1. Create task on this node
        resp = await client.post("/api/tasks", json={
            "task_id": "cross-1",
            "initiator_id": "user1",
            "content": {"desc": "cross-node test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # 2. Remote node (node-B) sends a bid via peer route
        resp = await client.post("/peer/task/bid", json={
            "task_id": "cross-1",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "node-B",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "executing"
        assert data["bid"]["agent_id"] == "a1"

        # Verify participant tracked
        participants = net.cluster.router.get_participants("cross-1")
        assert "node-B" in participants

        # 3. Remote node submits result via peer route
        resp = await client.post("/peer/task/result", json={
            "task_id": "cross-1",
            "agent_id": "a1",
            "content": "remote result",
            "from_node": "node-B",
        })
        assert resp.status_code == 200

        # Verify result stored
        task = net.task_manager.get("cross-1")
        assert len(task.results) == 1
        assert task.results[0].agent_id == "a1"
        assert task.results[0].content == "remote result"

        # 4. Close and select
        resp = await client.post("/api/tasks/cross-1/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 200

        resp = await client.post("/api/tasks/cross-1/select", json={
            "initiator_id": "user1",
            "agent_id": "a1",
        })
        assert resp.status_code == 200

    async def test_remote_broadcast_then_local_bid(self, two_node_client):
        """Remote node broadcasts task, local agents bid on it."""
        client, net = two_node_client

        # 1. Receive broadcast from remote node
        resp = await client.post("/peer/task/broadcast", json={
            "task_id": "remote-task-1",
            "origin": "node-B",
            "initiator_id": "remote-user",
            "domains": ["coding"],
            "budget": 200.0,
            "content": {"desc": "remote task"},
        })
        assert resp.status_code == 200

        # Verify route stored
        assert net.cluster.router.get_route("remote-task-1") == "node-B"
        assert net.cluster.router.is_local("remote-task-1") is False

    async def test_multiple_remote_bids_on_same_task(self, two_node_client):
        """Multiple remote nodes bid on the same local task."""
        client, net = two_node_client

        # Create task
        resp = await client.post("/api/tasks", json={
            "task_id": "multi-bid",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
            "max_concurrent_bidders": 3,
        })
        assert resp.status_code == 201

        # Add more agents to reputation
        net.reputation._scores.update({"a2": 0.85, "a3": 0.7})
        await net.dht.announce("coding", "a2")
        await net.dht.announce("coding", "a3")

        # Three remote bids
        for agent_id, from_node in [("a1", "node-B"), ("a2", "node-C"), ("a3", "node-D")]:
            resp = await client.post("/peer/task/bid", json={
                "task_id": "multi-bid",
                "agent_id": agent_id,
                "confidence": 0.9,
                "price": 80.0,
                "from_node": from_node,
            })
            assert resp.status_code == 200

        # All three participants tracked
        participants = net.cluster.router.get_participants("multi-bid")
        assert participants == {"node-B", "node-C", "node-D"}

    async def test_peer_reject_after_bid(self, two_node_client):
        """Remote node bids then rejects."""
        client, net = two_node_client

        resp = await client.post("/api/tasks", json={
            "task_id": "rej-test",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Bid
        resp = await client.post("/peer/task/bid", json={
            "task_id": "rej-test",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "node-B",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

        # Reject
        resp = await client.post("/peer/task/reject", json={
            "task_id": "rej-test",
            "agent_id": "a1",
            "from_node": "node-B",
        })
        assert resp.status_code == 200

        # Verify rejection
        task = net.task_manager.get("rej-test")
        bid = [b for b in task.bids if b.agent_id == "a1"][0]
        assert bid.status.value == "rejected"


class TestGossipExchangeViaAPI:
    @pytest.fixture
    async def gossip_client(self):
        db = Database()
        await db.connect()
        net = Network(db=db)
        await net.start()

        app = FastAPI()
        app.include_router(peer_router)
        set_peer_cluster(net.cluster)
        set_peer_network(net)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, net

        await db.close()

    async def test_gossip_exchange_via_api_adds_all_cards(self, gossip_client):
        client, net = gossip_client

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
                {
                    "node_id": "peer-z",
                    "endpoint": "http://pz:8000",
                    "domains": ["research"],
                    "version": "0.1.0",
                },
            ],
        })
        assert resp.status_code == 200
        data = resp.json()

        # All three peers should be in membership
        assert net.cluster.members.contains("peer-x") is True
        assert net.cluster.members.contains("peer-y") is True
        assert net.cluster.members.contains("peer-z") is True

        # Response should contain local nodes
        assert isinstance(data["known"], list)
        returned_ids = {n["node_id"] for n in data["known"]}
        assert net.cluster.node_id in returned_ids
        # peer-x excluded from response
        assert "peer-x" not in returned_ids

    async def test_gossip_exchange_updates_local_knowledge(self, gossip_client):
        client, net = gossip_client

        await client.post("/peer/gossip/exchange", json={
            "from_node": {
                "node_id": "info-source",
                "endpoint": "http://is:8000",
                "version": "0.1.0",
            },
            "known": [
                {
                    "node_id": "learned-peer",
                    "endpoint": "http://lp:8000",
                    "version": "0.1.0",
                },
            ],
        })

        # Local node should now know about info-source and learned-peer
        local_knows = await net.cluster.gossip.get_known(net.cluster.node_id)
        assert "info-source" in local_knows
        assert "learned-peer" in local_knows


class TestDHTMultiOperation:
    async def test_interleaved_announce_revoke(self, db):
        """Rapid announce/revoke interleaving."""
        dht = ClusterDHT(db)

        await dht.announce("d1", "n1")
        await dht.announce("d1", "n2")
        await dht.revoke("d1", "n1")
        await dht.announce("d1", "n3")
        await dht.revoke("d1", "n2")

        result = await dht.lookup("d1")
        assert result == ["n3"]

    async def test_announce_revoke_all_announce_again(self, db):
        dht = ClusterDHT(db)

        for i in range(10):
            await dht.announce(f"d{i}", "n1")

        await dht.revoke_all("n1")

        for i in range(10):
            assert await dht.lookup(f"d{i}") == []

        await dht.announce("d5", "n1")
        assert await dht.lookup("d5") == ["n1"]
        assert await dht.lookup("d0") == []

    async def test_multiple_domains_multiple_nodes(self, db):
        """Complex multi-domain, multi-node scenario."""
        dht = ClusterDHT(db)

        # 3 nodes, 3 domains, each node handles 2 domains
        await dht.announce("coding", "n1")
        await dht.announce("design", "n1")
        await dht.announce("design", "n2")
        await dht.announce("research", "n2")
        await dht.announce("research", "n3")
        await dht.announce("coding", "n3")

        assert set(await dht.lookup("coding")) == {"n1", "n3"}
        assert set(await dht.lookup("design")) == {"n1", "n2"}
        assert set(await dht.lookup("research")) == {"n2", "n3"}

        # Revoke n2's entries
        await dht.revoke_all("n2")
        assert set(await dht.lookup("coding")) == {"n1", "n3"}
        assert await dht.lookup("design") == ["n1"]
        assert await dht.lookup("research") == ["n3"]
