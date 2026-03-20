"""Tests for state consistency across cluster components.

Covers: route/participant cleanup on leave, gossip knowledge consistency,
DHT entries for offline nodes, membership-DHT alignment, stale state detection.
"""

import pytest
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.service import ClusterService
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.router import ClusterRouter
from eacn.network.config import ClusterConfig


class TestLeaveCleanup:
    """When a node leaves, dependent state should be consistent."""

    async def test_leave_removes_from_membership(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        cs.handle_join(peer)
        assert cs.members.contains("peer") is True

        cs.handle_leave("peer")
        assert cs.members.contains("peer") is False

    async def test_leave_preserves_other_members(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        p1 = NodeCard(node_id="p1", endpoint="http://p1:8000")
        p2 = NodeCard(node_id="p2", endpoint="http://p2:8000")
        cs.handle_join(p1)
        cs.handle_join(p2)

        cs.handle_leave("p1")
        assert cs.members.contains("p1") is False
        assert cs.members.contains("p2") is True
        assert cs.members.contains("local") is True

    async def test_leave_does_not_remove_routes(self, db):
        """Routes pointing to a left node remain (stale route scenario)."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        cs.handle_join(peer)
        cs.router.set_route("t1", "peer")

        cs.handle_leave("peer")

        # Route is still there — this is a known limitation
        # The system should handle this gracefully at forward time
        assert cs.router.get_route("t1") == "peer"
        assert cs.router.is_local("t1") is False

    async def test_leave_does_not_affect_dht(self, db):
        """DHT entries for a departed node remain until explicitly revoked."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        await cs.dht.announce("coding", "departed-node")
        cs.handle_leave("departed-node")

        # DHT still has the entry
        result = await cs.dht.lookup("coding")
        assert "departed-node" in result


class TestRouteStateConsistency:
    async def test_route_set_then_removed(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        assert router.is_local("t1") is False

        router.remove_route("t1")
        assert router.is_local("t1") is True
        assert router.get_route("t1") is None

    async def test_participants_survive_route_removal(self, db):
        """Participants should remain even if route is removed."""
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        router.add_participant("t1", "node-a")
        router.add_participant("t1", "node-b")

        router.remove_route("t1")

        # Participants still tracked
        assert router.get_participants("t1") == {"node-a", "node-b"}

    async def test_route_overwrite_changes_ownership(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "node-a")
        assert router.get_route("t1") == "node-a"
        assert router.is_local("t1") is False

        router.set_route("t1", "local")
        assert router.get_route("t1") == "local"
        assert router.is_local("t1") is True

    async def test_endpoint_and_route_independent(self, db):
        """Removing an endpoint doesn't affect routes and vice versa."""
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        router.set_endpoint("remote", "http://remote:8000")

        router.remove_route("t1")
        assert router.get_endpoint("remote") == "http://remote:8000"

    async def test_multiple_tasks_same_origin(self, db):
        """Multiple tasks can route to the same origin node."""
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        router.set_route("t2", "remote")
        router.set_route("t3", "remote")

        assert router.get_route("t1") == "remote"
        assert router.get_route("t2") == "remote"
        assert router.get_route("t3") == "remote"

        router.remove_route("t2")
        assert router.get_route("t1") == "remote"
        assert router.get_route("t2") is None
        assert router.get_route("t3") == "remote"


class TestGossipKnowledgeConsistency:
    async def test_exchange_keeps_knowledge_symmetric(self, db):
        """After exchange, both nodes know each other."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")

        a_knows = await gossip.get_known("a")
        b_knows = await gossip.get_known("b")
        assert "b" in a_knows
        assert "a" in b_knows

    async def test_chain_gossip_propagation(self, db):
        """A↔B, then B↔C → A should know C after B exchanges with C and then A."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")
        await gossip.exchange("b", "c")
        # Now B knows {a, c}. Exchange a and b again
        await gossip.exchange("a", "b")

        a_knows = await gossip.get_known("a")
        assert "b" in a_knows
        assert "c" in a_knows  # Propagated through B

    async def test_remove_node_cleans_all_knowledge(self, db):
        """Removing a node should clean it from all other nodes' known lists."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")
        await gossip.exchange("a", "c")

        # b knows a (from exchange), remove b
        await gossip.remove_node("b")

        a_knows = await gossip.get_known("a")
        assert "b" not in a_knows
        assert "c" in a_knows  # c unaffected

        b_knows = await gossip.get_known("b")
        assert b_knows == set()

    async def test_gossip_after_node_leave_doesnt_resurrect(self, db):
        """After removing a node from gossip, new exchanges shouldn't bring it back."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("a", "b")
        await gossip.exchange("a", "c")
        await gossip.remove_node("b")

        # a still knows c, exchange a and c — should NOT bring back b
        await gossip.exchange("a", "c")

        a_knows = await gossip.get_known("a")
        assert "b" not in a_knows
        assert "c" in a_knows


class TestDHTConsistency:
    async def test_revoke_then_announce_re_creates(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "n1")
        await dht.revoke("coding", "n1")
        assert await dht.lookup("coding") == []

        await dht.announce("coding", "n1")
        assert await dht.lookup("coding") == ["n1"]

    async def test_revoke_all_then_selective_announce(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "n1")
        await dht.announce("design", "n1")
        await dht.announce("research", "n1")

        await dht.revoke_all("n1")

        await dht.announce("coding", "n1")
        assert await dht.lookup("coding") == ["n1"]
        assert await dht.lookup("design") == []
        assert await dht.lookup("research") == []

    async def test_different_nodes_same_domain_independent(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "n1")
        await dht.announce("coding", "n2")
        await dht.announce("coding", "n3")

        await dht.revoke("coding", "n2")

        result = await dht.lookup("coding")
        assert set(result) == {"n1", "n3"}

    async def test_revoke_nonexistent_is_safe(self, db):
        dht = ClusterDHT(db)
        await dht.revoke("nonexistent", "nonexistent")
        assert await dht.lookup("nonexistent") == []

    async def test_revoke_all_nonexistent_is_safe(self, db):
        dht = ClusterDHT(db)
        await dht.revoke_all("nonexistent")
        # Should not affect other entries
        await dht.announce("coding", "n1")
        assert await dht.lookup("coding") == ["n1"]


class TestMembershipDHTAlignment:
    async def test_announce_domain_updates_both_node_and_dht(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        await cs.announce_domain("newdom")

        assert "newdom" in cs.local_node.domains
        dht_result = await cs.dht.lookup("newdom")
        assert "local" in dht_result

    async def test_revoke_domain_updates_both_node_and_dht(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        await cs.announce_domain("tempdom")
        await cs.revoke_domain("tempdom")

        assert "tempdom" not in cs.local_node.domains
        dht_result = await cs.dht.lookup("tempdom")
        assert "local" not in dht_result

    async def test_multiple_announces_revokes_consistent(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        for domain in ["a", "b", "c"]:
            await cs.announce_domain(domain)

        assert set(cs.local_node.domains) == {"a", "b", "c"}

        await cs.revoke_domain("b")
        assert set(cs.local_node.domains) == {"a", "c"}
        assert await cs.dht.lookup("a") == ["local"]
        assert await cs.dht.lookup("b") == []
        assert await cs.dht.lookup("c") == ["local"]


class TestHandleBroadcastConsistency:
    async def test_broadcast_sets_route_correctly(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        cs.handle_broadcast({"task_id": "t1", "origin": "remote-a", "domains": ["coding"]})
        cs.handle_broadcast({"task_id": "t2", "origin": "remote-b", "domains": ["design"]})

        assert cs.router.get_route("t1") == "remote-a"
        assert cs.router.get_route("t2") == "remote-b"
        assert cs.router.is_local("t1") is False
        assert cs.router.is_local("t2") is False

    async def test_broadcast_same_task_different_origin_overwrites(self, db):
        """Second broadcast with same task_id overwrites origin."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        cs.handle_broadcast({"task_id": "t1", "origin": "node-a", "domains": []})
        cs.handle_broadcast({"task_id": "t1", "origin": "node-b", "domains": []})

        # set_route overwrites
        assert cs.router.get_route("t1") == "node-b"

    async def test_broadcast_empty_fields_ignored(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        cs.handle_broadcast({"task_id": "", "origin": "", "domains": []})
        assert cs.router.get_route("") is None

        cs.handle_broadcast({"task_id": "t1", "origin": "", "domains": []})
        assert cs.router.get_route("t1") is None

        cs.handle_broadcast({"task_id": "", "origin": "node-a", "domains": []})
        assert cs.router.get_route("") is None


class TestJoinEndpointConsistency:
    async def test_join_sets_both_membership_and_router_endpoint(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:9000")
        cs.handle_join(peer)

        assert cs.members.contains("peer") is True
        assert cs.members.get("peer").endpoint == "http://peer:9000"
        assert cs.router.get_endpoint("peer") == "http://peer:9000"

    async def test_rejoin_with_same_endpoint_is_safe(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:9000")
        cs.handle_join(peer)
        cs.handle_join(peer)  # Same endpoint, no error

        assert cs.members.count() == 2  # local + peer
        assert cs.router.get_endpoint("peer") == "http://peer:9000"

    async def test_rejoin_with_different_endpoint_raises(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:9000")
        cs.handle_join(peer)

        conflict = NodeCard(node_id="peer", endpoint="http://different:9000")
        with pytest.raises(ValueError, match="already exists"):
            cs.handle_join(conflict)

        # Original preserved
        assert cs.members.get("peer").endpoint == "http://peer:9000"
        assert cs.router.get_endpoint("peer") == "http://peer:9000"

    async def test_leave_then_rejoin(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)

        peer = NodeCard(node_id="peer", endpoint="http://peer:9000")
        cs.handle_join(peer)
        cs.handle_leave("peer")
        assert cs.members.contains("peer") is False

        # Rejoin with different endpoint should work now
        new_peer = NodeCard(node_id="peer", endpoint="http://peer-new:9000")
        cs.handle_join(new_peer)
        assert cs.members.contains("peer") is True
        assert cs.members.get("peer").endpoint == "http://peer-new:9000"
