"""Tests for ClusterGossip."""

import pytest
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.node import NodeCard, MembershipList


class TestClusterGossip:
    async def test_exchange_merges_knowledge(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        # A knows C, B knows D
        await gossip.add_known("node-a", "node-c")
        await gossip.add_known("node-b", "node-d")

        await gossip.exchange("node-a", "node-b")

        a_knows = await gossip.get_known("node-a")
        b_knows = await gossip.get_known("node-b")
        # Both should know about each other, C, and D
        assert "node-b" in a_knows
        assert "node-c" in a_knows
        assert "node-d" in a_knows
        assert "node-a" in b_knows
        assert "node-c" in b_knows
        assert "node-d" in b_knows

    async def test_exchange_does_not_include_self(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.exchange("node-a", "node-b")
        a_knows = await gossip.get_known("node-a")
        assert "node-a" not in a_knows

    async def test_get_known_empty(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        known = await gossip.get_known("nonexistent")
        assert known == set()

    async def test_lookup_by_domain(self, db):
        members = MembershipList()
        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n2 = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["design"])
        n3 = NodeCard(node_id="n3", endpoint="http://n3:8000", domains=["coding", "design"])
        members.add(n1)
        members.add(n2)
        members.add(n3)

        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "n1")
        await gossip.add_known("local", "n2")
        await gossip.add_known("local", "n3")

        coding_nodes = await gossip.lookup("local", "coding")
        assert set(coding_nodes) == {"n1", "n3"}

        design_nodes = await gossip.lookup("local", "design")
        assert set(design_nodes) == {"n2", "n3"}

    async def test_lookup_excludes_offline(self, db):
        members = MembershipList()
        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n1.status = "offline"
        members.add(n1)

        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "n1")

        result = await gossip.lookup("local", "coding")
        assert result == []

    async def test_lookup_unknown_node_not_in_members(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "unknown-node")
        result = await gossip.lookup("local", "coding")
        assert result == []

    async def test_handle_exchange_adds_peer_to_members(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        peer = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["coding"])
        result = await gossip.handle_exchange(peer, [])
        assert members.contains("peer")

    async def test_handle_exchange_returns_local_nodes(self, db):
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        members.add(local)
        gossip = ClusterGossip(db, members)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        result = await gossip.handle_exchange(peer, [])
        node_ids = {n.node_id for n in result}
        assert "local" in node_ids

    async def test_handle_exchange_merges_known_cards(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        friend = NodeCard(node_id="friend", endpoint="http://friend:8000", domains=["x"])
        await gossip.handle_exchange(peer, [friend])

        assert members.contains("friend")

    async def test_remove_node(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("a", "b")
        await gossip.add_known("b", "a")
        await gossip.remove_node("b")
        a_knows = await gossip.get_known("a")
        assert "b" not in a_knows
        b_knows = await gossip.get_known("b")
        assert b_knows == set()

    async def test_add_known_idempotent(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("a", "b")
        await gossip.add_known("a", "b")
        known = await gossip.get_known("a")
        assert known == {"b"}
