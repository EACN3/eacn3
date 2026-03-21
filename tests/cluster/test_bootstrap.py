"""Tests for ClusterBootstrap."""

import pytest
from eacn3.network.cluster.bootstrap import ClusterBootstrap
from eacn3.network.config import ClusterConfig
from eacn3.network.cluster.node import NodeCard, MembershipList


class TestClusterBootstrap:
    def test_is_seed_when_endpoint_matches(self):
        local = NodeCard(node_id="n1", endpoint="http://seed:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        assert bs.is_seed is True

    def test_not_seed_when_endpoint_differs(self):
        local = NodeCard(node_id="n1", endpoint="http://other:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        assert bs.is_seed is False

    async def test_join_network_standalone_returns_empty(self):
        local = NodeCard(node_id="n1", endpoint="http://localhost:8000")
        members = MembershipList()
        config = ClusterConfig()  # No seed nodes
        bs = ClusterBootstrap(local, members, config)
        result = await bs.join_network()
        assert result == []

    def test_handle_join_adds_to_members_and_returns_list(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        new_node = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["coding"])
        result = bs.handle_join(new_node)

        # n2 must be added to members
        assert members.contains("n2") is True
        card = members.get("n2")
        assert card.endpoint == "http://n2:8000"
        assert card.domains == ["coding"]

        # Result must contain at least seed + new node
        result_ids = {n.node_id for n in result}
        assert "seed" in result_ids
        assert "n2" in result_ids
        assert len(result) == 2

    def test_handle_join_same_endpoint_idempotent(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        bs.handle_join(node)  # Same endpoint, no error
        assert members.contains("n2") is True
        assert members.count() == 2

    def test_handle_join_different_endpoint_raises(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        conflict = NodeCard(node_id="n2", endpoint="http://different:8000")
        with pytest.raises(ValueError, match="already exists with different endpoint"):
            bs.handle_join(conflict)

        # Original should still be intact
        assert members.get("n2").endpoint == "http://n2:8000"

    def test_handle_leave_removes_from_members(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000")
        bs.handle_join(node)
        assert members.count() == 2

        bs.handle_leave("n2")
        assert members.contains("n2") is False
        assert members.count() == 1

    def test_handle_heartbeat_updates_all_fields(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        node = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["old"])
        node.status = "suspect"  # simulate node was suspect
        bs.handle_join(node)

        bs.handle_heartbeat("n2", ["new_domain", "coding"], "2026-03-20T10:00:00Z")

        updated = members.get("n2")
        assert updated.domains == ["new_domain", "coding"]
        assert updated.last_seen == "2026-03-20T10:00:00Z"
        assert updated.status == "online"  # Heartbeat restores to online

    def test_handle_heartbeat_unknown_node_does_nothing(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)
        old_count = members.count()

        bs.handle_heartbeat("unknown", ["x"], "2026-01-01T00:00:00Z")

        # No phantom node created
        assert members.count() == old_count
        assert members.contains("unknown") is False

    def test_lookup_finds_correct_nodes(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        n1 = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding"])
        n2 = NodeCard(node_id="n2", endpoint="http://n2:8000", domains=["coding", "design"])
        n3 = NodeCard(node_id="n3", endpoint="http://n3:8000", domains=["design"])
        bs.handle_join(n1)
        bs.handle_join(n2)
        bs.handle_join(n3)

        coding = bs.lookup("coding")
        assert set(coding) == {"n1", "n2"}  # Excludes seed (local)

        design = bs.lookup("design")
        assert set(design) == {"n2", "n3"}

    def test_lookup_excludes_local_node(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000", domains=["coding"])
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        found = bs.lookup("coding")
        assert found == []
        assert "seed" not in found
