"""Tests for NodeCard and MembershipList."""

import pytest
from eacn3.network.cluster.node import NodeCard, MembershipList


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
        assert card.joined_at != ""
        assert card.last_seen != ""

    def test_to_dict_roundtrip(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["a", "b"])
        d = card.to_dict()
        assert d["node_id"] == "n1"
        assert d["endpoint"] == "http://n1:8000"
        assert d["domains"] == ["a", "b"]
        assert d["status"] == "online"

        restored = NodeCard.from_dict(d)
        assert restored.node_id == "n1"
        assert restored.endpoint == "http://n1:8000"
        assert restored.domains == ["a", "b"]
        assert restored.status == "online"
        assert restored.version == card.version
        assert restored.joined_at == card.joined_at
        assert restored.last_seen == card.last_seen

    def test_default_status_is_online(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        assert card.status == "online"

    def test_default_domains_is_empty(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        assert card.domains == []

    def test_to_dict_contains_all_fields(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        d = card.to_dict()
        expected_keys = {"node_id", "endpoint", "domains", "status", "version", "joined_at", "last_seen"}
        assert set(d.keys()) == expected_keys


class TestMembershipList:
    def test_add_and_get(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        got = ml.get(local_node.node_id)
        assert got is local_node
        assert got.node_id == "node-local"
        assert got.endpoint == "http://localhost:8000"
        assert ml.count() == 1

    def test_remove_returns_card(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        removed = ml.remove(local_node.node_id)
        assert removed is local_node
        assert removed.node_id == "node-local"
        assert ml.get(local_node.node_id) is None
        assert ml.count() == 0

    def test_remove_nonexistent_returns_none(self):
        ml = MembershipList()
        result = ml.remove("nonexistent")
        assert result is None

    def test_all_online_returns_only_online(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        online = ml.all_online()
        assert len(online) == 2
        ids = {n.node_id for n in online}
        assert ids == {"node-local", "node-peer"}

    def test_all_online_excludes_offline_nodes(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "offline"
        online = ml.all_online()
        assert len(online) == 1
        assert online[0].node_id == "node-local"

    def test_all_online_excludes_suspect_nodes(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "suspect"
        online = ml.all_online()
        assert len(online) == 1
        assert online[0].node_id == "node-local"

    def test_all_online_with_exclude(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        online = ml.all_online(exclude="node-local")
        assert len(online) == 1
        assert online[0].node_id == "node-peer"

    def test_all_nodes_includes_offline(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "offline"
        all_nodes = ml.all_nodes()
        assert len(all_nodes) == 2
        ids = {n.node_id for n in all_nodes}
        assert ids == {"node-local", "node-peer"}

    def test_all_nodes_with_exclude(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        result = ml.all_nodes(exclude="node-local")
        assert len(result) == 1
        assert result[0].node_id == "node-peer"

    def test_find_by_domain_shared_domain(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        # Both have "translation" per conftest
        found = ml.find_by_domain("translation")
        assert set(found) == {"node-local", "node-peer"}

    def test_find_by_domain_unique_to_one(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        # Only peer has "design"
        found = ml.find_by_domain("design")
        assert found == ["node-peer"]

    def test_find_by_domain_excludes_self(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        found = ml.find_by_domain("translation", exclude="node-local")
        assert found == ["node-peer"]

    def test_find_by_domain_excludes_offline(self, local_node, peer_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.add(peer_node)
        peer_node.status = "suspect"
        found = ml.find_by_domain("translation")
        assert found == ["node-local"]

    def test_find_by_domain_no_match(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        found = ml.find_by_domain("nonexistent")
        assert found == []

    def test_update_last_seen_changes_value(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        old_ts = local_node.last_seen
        ml.update_last_seen("node-local", "2026-01-01T00:00:00Z")
        assert ml.get("node-local").last_seen == "2026-01-01T00:00:00Z"
        assert ml.get("node-local").last_seen != old_ts

    def test_update_last_seen_auto_timestamp(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        ml.update_last_seen("node-local")
        assert ml.get("node-local").last_seen != ""

    def test_update_domains_replaces_list(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        old = list(local_node.domains)
        ml.update_domains("node-local", ["new_domain"])
        assert ml.get("node-local").domains == ["new_domain"]
        assert ml.get("node-local").domains != old

    def test_update_status_changes_value(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        assert ml.get("node-local").status == "online"
        ml.update_status("node-local", "suspect")
        assert ml.get("node-local").status == "suspect"

    def test_contains_true_and_false(self, local_node):
        ml = MembershipList()
        assert ml.contains("node-local") is False
        ml.add(local_node)
        assert ml.contains("node-local") is True

    def test_update_on_nonexistent_does_nothing(self):
        ml = MembershipList()
        # These must not raise AND must not create phantom entries
        ml.update_last_seen("ghost")
        ml.update_domains("ghost", ["x"])
        ml.update_status("ghost", "offline")
        assert ml.get("ghost") is None
        assert ml.count() == 0

    def test_add_replaces_existing(self, local_node):
        ml = MembershipList()
        ml.add(local_node)
        replacement = NodeCard(
            node_id="node-local",
            endpoint="http://new:9000",
            domains=["new"],
        )
        ml.add(replacement)
        got = ml.get("node-local")
        assert got.endpoint == "http://new:9000"
        assert got.domains == ["new"]
        assert ml.count() == 1
