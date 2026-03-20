"""Tests for ClusterService."""

import pytest
from eacn.network.config import ClusterConfig
from eacn.network.cluster.service import ClusterService
from eacn.network.cluster.node import NodeCard


class TestStandaloneMode:
    async def test_standalone_when_no_seeds(self, db):
        config = ClusterConfig(node_id="n1", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.standalone is True
        assert cs.config.seed_nodes == []

    async def test_not_standalone_when_seeds(self, db):
        config = ClusterConfig(
            node_id="n1",
            endpoint="http://n1:8000",
            seed_nodes=["http://seed:8000"],
        )
        cs = ClusterService(db, config=config)
        assert cs.standalone is False

    async def test_start_standalone(self, standalone_cluster):
        cs = standalone_cluster
        assert cs.standalone is True
        assert cs.members.count() == 1
        assert cs.members.contains(cs.node_id) is True

    async def test_stop_standalone(self, standalone_cluster):
        await standalone_cluster.stop()
        # Should complete without error; still has self in members
        assert standalone_cluster.members.contains(standalone_cluster.node_id) is True

    async def test_broadcast_task_standalone_returns_empty(self, standalone_cluster):
        result = await standalone_cluster.broadcast_task({
            "task_id": "t1", "domains": ["coding"],
        })
        assert result == []

    async def test_trigger_gossip_standalone(self, standalone_cluster):
        # In standalone mode, gossip should be a safe no-op
        standalone_cluster.router.add_participant("t1", "some-node")
        await standalone_cluster.trigger_gossip("t1")
        # No gossip exchange should happen (standalone)
        local_knows = await standalone_cluster.gossip.get_known(standalone_cluster.node_id)
        assert "some-node" not in local_knows


class TestClusterServiceInit:
    async def test_auto_generated_node_id(self, db):
        config = ClusterConfig(endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert len(cs.node_id) == 36  # UUID format

    async def test_explicit_node_id(self, db):
        config = ClusterConfig(node_id="my-node", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.node_id == "my-node"

    async def test_local_node_in_members(self, db):
        config = ClusterConfig(node_id="n1", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.members.contains("n1") is True
        card = cs.members.get("n1")
        assert card.endpoint == "http://n1:8000"
        assert card.version == "0.1.0"

    async def test_submodules_are_properly_wired(self, db):
        config = ClusterConfig(node_id="n1")
        cs = ClusterService(db, config=config)
        assert cs.bootstrap is not None
        assert cs.dht is not None
        assert cs.gossip is not None
        assert cs.discovery is not None
        assert cs.router is not None
        # gossip should have the local node id
        assert cs.gossip._local_node_id == "n1"


class TestDomainManagement:
    async def test_announce_domain_adds_to_node_and_dht(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("new_domain")

        assert "new_domain" in cs.local_node.domains
        result = await cs.dht.lookup("new_domain")
        assert cs.node_id in result

    async def test_announce_domain_idempotent(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("x")
        await cs.announce_domain("x")

        assert cs.local_node.domains.count("x") == 1
        result = await cs.dht.lookup("x")
        assert result == [cs.node_id]

    async def test_revoke_domain_removes_from_node_and_dht(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("temp")
        await cs.revoke_domain("temp")

        assert "temp" not in cs.local_node.domains
        result = await cs.dht.lookup("temp")
        assert cs.node_id not in result

    async def test_revoke_domain_not_present(self, standalone_cluster):
        cs = standalone_cluster
        original_domains = list(cs.local_node.domains)
        await cs.revoke_domain("nonexistent")
        # No change to domains
        assert cs.local_node.domains == original_domains


class TestPeerHandlers:
    async def test_handle_join_adds_member_and_endpoint(self, cluster_with_peers):
        cs = cluster_with_peers
        initial_count = cs.members.count()
        new_node = NodeCard(node_id="new", endpoint="http://new:8000", domains=["research"])

        nodes = cs.handle_join(new_node)

        assert cs.members.contains("new") is True
        assert cs.members.get("new").endpoint == "http://new:8000"
        assert cs.members.get("new").domains == ["research"]
        assert cs.router.get_endpoint("new") == "http://new:8000"
        assert cs.members.count() == initial_count + 1

        # Returned list should include the new node
        returned_ids = {n.node_id for n in nodes}
        assert "new" in returned_ids

    async def test_handle_leave_removes_member(self, cluster_with_peers):
        cs = cluster_with_peers
        assert cs.members.contains("node-peer") is True
        cs.handle_leave("node-peer")
        assert cs.members.contains("node-peer") is False

    async def test_handle_heartbeat_updates_node(self, cluster_with_peers):
        cs = cluster_with_peers
        old_domains = list(cs.members.get("node-peer").domains)

        cs.handle_heartbeat("node-peer", ["coding", "writing"], "2026-03-20T12:00:00Z")

        node = cs.members.get("node-peer")
        assert node.domains == ["coding", "writing"]
        assert node.domains != old_domains
        assert node.last_seen == "2026-03-20T12:00:00Z"
        assert node.status == "online"

    async def test_handle_broadcast_stores_route(self, cluster_with_peers):
        cs = cluster_with_peers
        cs.handle_broadcast({
            "task_id": "t1",
            "origin": "node-peer",
            "domains": ["coding"],
        })

        assert cs.router.get_route("t1") == "node-peer"
        assert cs.router.is_local("t1") is False

    async def test_handle_broadcast_empty_task_id_ignored(self, cluster_with_peers):
        cs = cluster_with_peers
        cs.handle_broadcast({"task_id": "", "origin": "", "domains": []})
        # Empty task_id should not create a route
        assert cs.router.get_route("") is None

    async def test_handle_push_returns_recipient_count(self, cluster_with_peers):
        cs = cluster_with_peers
        delivered = await cs.handle_push("TASK_BROADCAST", "t1", ["a1", "a2", "a3"], {})
        assert delivered == 3

    async def test_handle_push_empty_recipients(self, cluster_with_peers):
        cs = cluster_with_peers
        delivered = await cs.handle_push("TASK_BROADCAST", "t1", [], {})
        assert delivered == 0


class TestTriggerGossip:
    async def test_gossip_with_participants(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False  # Force non-standalone for testing

        cs.router.add_participant("t1", "node-a")

        await cs.trigger_gossip("t1")

        # After gossip exchange, local should know about node-a
        local_knows = await cs.gossip.get_known("local")
        assert "node-a" in local_knows

    async def test_gossip_skips_self(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        cs.router.add_participant("t1", "local")  # Self as participant
        cs.router.add_participant("t1", "node-a")

        await cs.trigger_gossip("t1")

        local_knows = await cs.gossip.get_known("local")
        assert "node-a" in local_knows
        assert "local" not in local_knows  # Never know about self

    async def test_gossip_no_participants(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        await cs.trigger_gossip("t1")

        local_knows = await cs.gossip.get_known("local")
        assert local_knows == set()
