"""Tests for ClusterService."""

import pytest
from eacn.network.cluster.service import ClusterService
from eacn.network.cluster.config import ClusterConfig
from eacn.network.cluster.node import NodeCard


class TestStandaloneMode:
    async def test_standalone_when_no_seeds(self, db):
        config = ClusterConfig(node_id="n1", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.standalone

    async def test_not_standalone_when_seeds(self, db):
        config = ClusterConfig(
            node_id="n1",
            endpoint="http://n1:8000",
            seed_nodes=["http://seed:8000"],
        )
        cs = ClusterService(db, config=config)
        assert not cs.standalone

    async def test_start_standalone_no_error(self, standalone_cluster):
        # Already started in fixture, just verify it works
        assert standalone_cluster.standalone
        assert standalone_cluster.members.count() == 1

    async def test_stop_standalone_no_error(self, standalone_cluster):
        await standalone_cluster.stop()  # Should not raise

    async def test_broadcast_task_standalone_returns_empty(self, standalone_cluster):
        result = await standalone_cluster.broadcast_task({
            "task_id": "t1", "domains": ["coding"],
        })
        assert result == []

    async def test_trigger_gossip_standalone_no_op(self, standalone_cluster):
        await standalone_cluster.trigger_gossip("t1")  # Should not raise


class TestClusterServiceInit:
    async def test_auto_generated_node_id(self, db):
        config = ClusterConfig(endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.node_id  # Should be auto-generated UUID
        assert len(cs.node_id) > 0

    async def test_explicit_node_id(self, db):
        config = ClusterConfig(node_id="my-node", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.node_id == "my-node"

    async def test_local_node_in_members(self, db):
        config = ClusterConfig(node_id="n1", endpoint="http://n1:8000")
        cs = ClusterService(db, config=config)
        assert cs.members.contains("n1")

    async def test_submodules_initialized(self, db):
        config = ClusterConfig(node_id="n1")
        cs = ClusterService(db, config=config)
        assert cs.bootstrap is not None
        assert cs.dht is not None
        assert cs.gossip is not None
        assert cs.discovery is not None
        assert cs.router is not None


class TestDomainManagement:
    async def test_announce_domain(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("new_domain")
        assert "new_domain" in cs.local_node.domains
        # Also in DHT
        result = await cs.dht.lookup("new_domain")
        assert cs.node_id in result

    async def test_announce_domain_idempotent(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("x")
        await cs.announce_domain("x")
        assert cs.local_node.domains.count("x") == 1

    async def test_revoke_domain(self, standalone_cluster):
        cs = standalone_cluster
        await cs.announce_domain("temp")
        await cs.revoke_domain("temp")
        assert "temp" not in cs.local_node.domains
        result = await cs.dht.lookup("temp")
        assert cs.node_id not in result

    async def test_revoke_domain_not_present(self, standalone_cluster):
        cs = standalone_cluster
        await cs.revoke_domain("nonexistent")  # Should not raise


class TestPeerHandlers:
    async def test_handle_join(self, cluster_with_peers):
        cs = cluster_with_peers
        new_node = NodeCard(node_id="new", endpoint="http://new:8000")
        nodes = cs.handle_join(new_node)
        assert cs.members.contains("new")
        assert cs.router.get_endpoint("new") == "http://new:8000"
        assert len(nodes) > 0

    async def test_handle_leave(self, cluster_with_peers):
        cs = cluster_with_peers
        cs.handle_leave("node-peer")
        assert not cs.members.contains("node-peer")

    async def test_handle_heartbeat(self, cluster_with_peers):
        cs = cluster_with_peers
        cs.handle_heartbeat("node-peer", ["coding", "writing"], "2026-03-20T12:00:00Z")
        node = cs.members.get("node-peer")
        assert node.domains == ["coding", "writing"]
        assert node.last_seen == "2026-03-20T12:00:00Z"

    async def test_handle_broadcast_stores_route(self, cluster_with_peers):
        cs = cluster_with_peers
        cs.handle_broadcast({
            "task_id": "t1",
            "origin": "node-peer",
            "domains": ["coding"],
        })
        assert cs.router.get_route("t1") == "node-peer"
        assert not cs.router.is_local("t1")

    async def test_handle_status_notification(self, cluster_with_peers):
        cs = cluster_with_peers
        # Should not raise
        await cs.handle_status_notification("t1", "completed", {})

    async def test_handle_push(self, cluster_with_peers):
        cs = cluster_with_peers
        delivered = await cs.handle_push("TASK_BROADCAST", "t1", ["a1"], {})
        assert delivered == 1


class TestTriggerGossip:
    async def test_gossip_with_participants(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        # Not standalone but no real network, just test the gossip exchange logic
        cs._standalone = False
        cs.router.add_participant("t1", "node-a")
        cs.router.add_participant("t1", "node-b")
        # Should not raise even though nodes are not in membership
        await cs.trigger_gossip("t1")
