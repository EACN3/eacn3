"""Tests for ClusterBootstrap."""

import pytest
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.config import ClusterConfig
from eacn.network.cluster.node import NodeCard, MembershipList


class TestClusterBootstrap:
    def test_is_seed_when_endpoint_matches(self):
        local = NodeCard(node_id="n1", endpoint="http://seed:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        assert bs.is_seed

    def test_not_seed_when_endpoint_differs(self):
        local = NodeCard(node_id="n1", endpoint="http://other:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        assert not bs.is_seed

    async def test_join_network_standalone_returns_empty(self):
        local = NodeCard(node_id="n1", endpoint="http://localhost:8000")
        members = MembershipList()
        config = ClusterConfig()  # No seed nodes
        bs = ClusterBootstrap(local, members, config)
        result = await bs.join_network()
        assert result == []

    def test_handle_join_adds_to_members(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        new_node = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["coding"])
        result = bs.handle_join(new_node)
        assert members.contains("n2")
        assert len(result) >= 1

    def test_handle_join_duplicate_same_endpoint(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        # Joining again with same endpoint should not raise
        bs.handle_join(node)
        assert members.contains("n2")

    def test_handle_join_duplicate_different_endpoint_raises(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        conflict = NodeCard(node_id="n2", endpoint="http://n2-different:8000")
        with pytest.raises(ValueError, match="already exists"):
            bs.handle_join(conflict)

    def test_handle_leave_removes_from_members(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        bs.handle_leave("n2")
        assert not members.contains("n2")

    def test_handle_heartbeat_updates_member(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["old"])
        bs.handle_join(node)
        bs.handle_heartbeat("n2", ["new_domain"], "2026-03-20T10:00:00Z")

        updated = members.get("n2")
        assert updated.domains == ["new_domain"]
        assert updated.last_seen == "2026-03-20T10:00:00Z"
        assert updated.status == "online"

    def test_handle_heartbeat_unknown_node_ignored(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        # Should not raise
        bs.handle_heartbeat("unknown", ["x"], "2026-01-01T00:00:00Z")

    def test_lookup_by_domain(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n2 = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["coding", "design"])
        bs.handle_join(n1)
        bs.handle_join(n2)

        found = bs.lookup("coding")
        assert set(found) == {"n1", "n2"}  # Excludes seed (local)

    def test_lookup_excludes_local(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000", domains=["coding"])
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        found = bs.lookup("coding")
        assert found == []
