"""Tests for ClusterGossip."""

import pytest
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.node import NodeCard, MembershipList


class TestExchange:
    async def test_exchange_merges_knowledge_bidirectionally(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        # A knows C, B knows D
        await gossip.add_known("node-a", "node-c")
        await gossip.add_known("node-b", "node-d")

        await gossip.exchange("node-a", "node-b")

        a_knows = await gossip.get_known("node-a")
        b_knows = await gossip.get_known("node-b")

        # A must know: B, C, D (not self)
        assert a_knows == {"node-b", "node-c", "node-d"}
        # B must know: A, C, D (not self)
        assert b_knows == {"node-a", "node-c", "node-d"}

    async def test_exchange_self_exclusion(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.exchange("node-a", "node-b")

        a_knows = await gossip.get_known("node-a")
        b_knows = await gossip.get_known("node-b")
        assert "node-a" not in a_knows
        assert "node-b" not in b_knows
        assert "node-b" in a_knows
        assert "node-a" in b_knows

    async def test_exchange_already_shared_knowledge(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        # Both already know node-c
        await gossip.add_known("node-a", "node-c")
        await gossip.add_known("node-b", "node-c")

        await gossip.exchange("node-a", "node-b")

        a_knows = await gossip.get_known("node-a")
        assert a_knows == {"node-b", "node-c"}


class TestGetKnown:
    async def test_returns_correct_set(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("x", "y")
        await gossip.add_known("x", "z")
        assert await gossip.get_known("x") == {"y", "z"}

    async def test_empty_for_unknown_node(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        result = await gossip.get_known("nonexistent")
        assert result == set()


class TestLookup:
    async def test_finds_nodes_by_domain(self, db):
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

    async def test_excludes_offline_nodes(self, db):
        members = MembershipList()
        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n1.status = "offline"
        members.add(n1)

        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "n1")

        result = await gossip.lookup("local", "coding")
        assert result == []

    async def test_excludes_suspect_nodes(self, db):
        members = MembershipList()
        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n1.status = "suspect"
        members.add(n1)

        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "n1")
        assert await gossip.lookup("local", "coding") == []

    async def test_ignores_known_but_not_in_members(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "ghost-node")
        result = await gossip.lookup("local", "coding")
        assert result == []

    async def test_no_results_for_wrong_domain(self, db):
        members = MembershipList()
        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        members.add(n1)
        gossip = ClusterGossip(db, members)
        await gossip.add_known("local", "n1")
        assert await gossip.lookup("local", "design") == []


class TestHandleExchange:
    async def test_adds_peer_to_membership(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        peer = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["coding"])

        assert members.contains("peer") is False
        await gossip.handle_exchange(peer, [])
        assert members.contains("peer") is True
        assert members.get("peer").endpoint == "http://peer:8000"
        assert members.get("peer").domains == ["coding"]

    async def test_adds_known_cards_to_membership(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        friend = NodeCard(node_id="friend", endpoint="http://friend:8000", domains=["x"])

        await gossip.handle_exchange(peer, [friend])

        assert members.contains("peer") is True
        assert members.contains("friend") is True
        assert members.get("friend").domains == ["x"]

    async def test_returns_all_local_nodes_except_peer(self, db):
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        n2 = NodeCard(node_id="n2", endpoint="http://n2:8000")
        members.add(local)
        members.add(n2)
        gossip = ClusterGossip(db, members)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        result = await gossip.handle_exchange(peer, [])

        result_ids = {n.node_id for n in result}
        assert "local" in result_ids
        assert "n2" in result_ids
        assert "peer" not in result_ids

    async def test_updates_local_gossip_knowledge(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members, local_node_id="local")

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        friend = NodeCard(node_id="friend", endpoint="http://friend:8000")
        await gossip.handle_exchange(peer, [friend])

        # Local should now know about peer and friend
        local_knows = await gossip.get_known("local")
        assert "peer" in local_knows
        assert "friend" in local_knows

    async def test_stores_peer_gossip_knowledge(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        friend = NodeCard(node_id="friend", endpoint="http://friend:8000")
        await gossip.handle_exchange(peer, [friend])

        # Peer should know about friend
        peer_knows = await gossip.get_known("peer")
        assert "friend" in peer_knows

    async def test_does_not_duplicate_existing_member(self, db):
        members = MembershipList()
        existing = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["old"])
        members.add(existing)
        gossip = ClusterGossip(db, members)

        peer_again = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["new"])
        await gossip.handle_exchange(peer_again, [])
        # Existing card should NOT be replaced because contains() returned True
        assert members.get("peer").domains == ["old"]


class TestRemoveNode:
    async def test_removes_from_both_directions(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("a", "b")
        await gossip.add_known("b", "a")
        await gossip.add_known("a", "c")

        await gossip.remove_node("b")

        a_knows = await gossip.get_known("a")
        assert "b" not in a_knows
        assert "c" in a_knows  # c should remain

        b_knows = await gossip.get_known("b")
        assert b_knows == set()


class TestAddKnown:
    async def test_idempotent(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members)
        await gossip.add_known("a", "b")
        await gossip.add_known("a", "b")
        known = await gossip.get_known("a")
        assert known == {"b"}
