"""Tests for NodeCard and MembershipList."""

import pytest
from eacn.network.cluster.node import NodeCard, MembershipList


class TestNodeCard:
    def test_create_node_card(self):
        card = NodeCard(
            node_id="n1",
            endpoint="http://n1:8000",
            domains=["coding"],
            version="0.1.0",
        )
        assert card.node_id == "n1"
        assert card.endpoint == "http://n1:8000"
        assert card.domains == ["coding"]
        assert card.status == "online"
        assert card.version == "0.1.0"
        assert card.joined_at
        assert card.last_seen

    def test_to_dict_and_from_dict(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["a", "b"])
        d = card.to_dict()
        assert d["node_id"] == "n1"
        assert d["domains"] == ["a", "b"]

        restored = NodeCard.from_dict(d)
        assert restored.node_id == card.node_id
        assert restored.endpoint == card.endpoint
        assert restored.domains == card.domains

    def test_default_status_is_online(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        assert card.status == "online"

    def test_default_domains_is_empty(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        assert card.domains == []


class TestMembershipList:
    def test_add_and_get(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        assert ml.get(local_node.node_id) is local_node
        assert ml.count() == 1

    def test_remove(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        removed = ml.remove(local_node.node_id)
        assert removed is local_node
        assert ml.get(local_node.node_id) is None
        assert ml.count() == 0

    def test_remove_nonexistent(self):
        ml = MembershipList()
        result = ml.remove("nonexistent")
        assert result is None

    def test_all_online(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        online = ml.all_online()
        assert len(online) == 2

    def test_all_online_excludes_offline(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "offline"
        online = ml.all_online()
        assert len(online) == 1
        assert online[0].node_id == local_node.node_id

    def test_all_online_with_exclude(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        online = ml.all_online(exclude=local_node.node_id)
        assert len(online) == 1
        assert online[0].node_id == peer_node.node_id

    def test_all_nodes(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "offline"
        all_nodes = ml.all_nodes()
        assert len(all_nodes) == 2

    def test_all_nodes_with_exclude(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        result = ml.all_nodes(exclude=local_node.node_id)
        assert len(result) == 1

    def test_find_by_domain(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        # Both have "translation"
        found = ml.find_by_domain("translation")
        assert set(found) == {local_node.node_id, peer_node.node_id}

    def test_find_by_domain_excludes_self(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        found = ml.find_by_domain("translation", exclude=local_node.node_id)
        assert found == [peer_node.node_id]

    def test_find_by_domain_excludes_offline(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "suspect"
        found = ml.find_by_domain("translation")
        assert found == [local_node.node_id]

    def test_find_by_domain_no_match(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        found = ml.find_by_domain("nonexistent")
        assert found == []

    def test_update_last_seen(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.update_last_seen(local_node.node_id, "2026-01-01T00:00:00Z")
        assert ml.get(local_node.node_id).last_seen == "2026-01-01T00:00:00Z"

    def test_update_domains(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.update_domains(local_node.node_id, ["new_domain"])
        assert ml.get(local_node.node_id).domains == ["new_domain"]

    def test_update_status(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.update_status(local_node.node_id, "suspect")
        assert ml.get(local_node.node_id).status == "suspect"

    def test_contains(self, local_node):
        ml = MembershipList()
        assert not ml.contains(local_node.node_id)
        ml.add(local_node)
        assert ml.contains(local_node.node_id)

    def test_update_on_nonexistent_is_safe(self):
        ml = MembershipList()
        # These should not raise
        ml.update_last_seen("nonexistent")
        ml.update_domains("nonexistent", [])
        ml.update_status("nonexistent", "offline")
