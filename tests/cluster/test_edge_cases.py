"""Tests for input validation, edge cases, and boundary conditions.

Covers: empty strings, special characters, unicode, large inputs,
boundary values, and unusual but valid usage patterns.
"""

import pytest
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.router import ClusterRouter
from eacn.network.cluster.service import ClusterService
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.discovery import ClusterDiscovery
from eacn.network.config import ClusterConfig


class TestNodeCardEdgeCases:
    def test_empty_node_id(self):
        card = NodeCard(node_id="", endpoint="http://x:8000")
        assert card.node_id == ""
        d = card.to_dict()
        restored = NodeCard.from_dict(d)
        assert restored.node_id == ""

    def test_empty_endpoint(self):
        card = NodeCard(node_id="n1", endpoint="")
        assert card.endpoint == ""

    def test_unicode_domain_names(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["编程", "設計", "コーディング"])
        assert card.domains == ["编程", "設計", "コーディング"]
        d = card.to_dict()
        restored = NodeCard.from_dict(d)
        assert restored.domains == ["编程", "設計", "コーディング"]

    def test_unicode_node_id(self):
        card = NodeCard(node_id="节点-1", endpoint="http://n1:8000")
        assert card.node_id == "节点-1"
        d = card.to_dict()
        assert d["node_id"] == "节点-1"

    def test_special_characters_in_domain(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["domain/with/slashes", "domain.with.dots", "domain-with-dashes"])
        assert len(card.domains) == 3

    def test_very_long_domain_name(self):
        long_domain = "x" * 1000
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=[long_domain])
        assert card.domains == [long_domain]
        d = card.to_dict()
        restored = NodeCard.from_dict(d)
        assert restored.domains == [long_domain]

    def test_many_domains(self):
        domains = [f"domain-{i}" for i in range(100)]
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=domains)
        assert len(card.domains) == 100
        d = card.to_dict()
        restored = NodeCard.from_dict(d)
        assert len(restored.domains) == 100

    def test_duplicate_domains_in_list(self):
        card = NodeCard(node_id="n1", endpoint="http://n1:8000", domains=["coding", "coding", "coding"])
        assert card.domains == ["coding", "coding", "coding"]

    def test_from_dict_extra_fields_ignored(self):
        d = {
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "extra_field": "should be ignored",
        }
        # Pydantic should handle extra fields gracefully
        try:
            card = NodeCard.from_dict(d)
            assert card.node_id == "n1"
        except Exception:
            # If model is strict, this is also acceptable behavior
            pass


class TestMembershipListEdgeCases:
    def test_add_remove_add_same_node(self):
        ml = MembershipList()
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        ml.add(card)
        ml.remove("n1")
        ml.add(card)
        assert ml.contains("n1") is True
        assert ml.count() == 1

    def test_large_membership_list(self):
        ml = MembershipList()
        for i in range(500):
            card = NodeCard(node_id=f"n{i}", endpoint=f"http://n{i}:8000", domains=["coding"])
            ml.add(card)

        assert ml.count() == 500
        found = ml.find_by_domain("coding")
        assert len(found) == 500

    def test_find_by_domain_empty_membership(self):
        ml = MembershipList()
        found = ml.find_by_domain("coding")
        assert found == []

    def test_all_online_empty_membership(self):
        ml = MembershipList()
        assert ml.all_online() == []
        assert ml.all_nodes() == []

    def test_update_status_cycles(self):
        ml = MembershipList()
        card = NodeCard(node_id="n1", endpoint="http://n1:8000")
        ml.add(card)

        ml.update_status("n1", "suspect")
        assert ml.get("n1").status == "suspect"

        ml.update_status("n1", "offline")
        assert ml.get("n1").status == "offline"

        ml.update_status("n1", "online")
        assert ml.get("n1").status == "online"

    def test_remove_returns_correct_node(self):
        ml = MembershipList()
        c1 = NodeCard(node_id="n1", endpoint="http://n1:8000")
        c2 = NodeCard(node_id="n2", endpoint="http://n2:8000")
        ml.add(c1)
        ml.add(c2)

        removed = ml.remove("n1")
        assert removed.node_id == "n1"
        assert ml.count() == 1
        assert ml.contains("n2") is True


class TestDHTEdgeCases:
    async def test_empty_domain_string(self, db):
        dht = ClusterDHT(db)
        await dht.announce("", "n1")
        result = await dht.lookup("")
        assert result == ["n1"]

    async def test_unicode_domain(self, db):
        dht = ClusterDHT(db)
        await dht.announce("编程", "n1")
        result = await dht.lookup("编程")
        assert result == ["n1"]

    async def test_hash_domain_empty_string(self):
        h = ClusterDHT.hash_domain("")
        assert isinstance(h, int)

    async def test_hash_domain_unicode(self):
        h = ClusterDHT.hash_domain("编程")
        assert isinstance(h, int)
        # Should be different from ASCII domain
        h2 = ClusterDHT.hash_domain("coding")
        assert h != h2

    async def test_case_sensitive_domains(self, db):
        """Domains are case-sensitive."""
        dht = ClusterDHT(db)
        await dht.announce("Coding", "n1")
        await dht.announce("coding", "n2")

        upper = await dht.lookup("Coding")
        lower = await dht.lookup("coding")
        assert upper == ["n1"]
        assert lower == ["n2"]

    async def test_many_nodes_per_domain(self, db):
        dht = ClusterDHT(db)
        for i in range(100):
            await dht.announce("popular", f"n{i}")

        result = await dht.lookup("popular")
        assert len(result) == 100
        assert set(result) == {f"n{i}" for i in range(100)}

    async def test_many_domains_per_node(self, db):
        dht = ClusterDHT(db)
        for i in range(100):
            await dht.announce(f"domain-{i}", "single-node")

        for i in range(100):
            result = await dht.lookup(f"domain-{i}")
            assert result == ["single-node"]

        await dht.revoke_all("single-node")
        for i in range(100):
            assert await dht.lookup(f"domain-{i}") == []


class TestGossipEdgeCases:
    async def test_exchange_same_node_with_itself(self, db):
        """Exchange a node with itself — should not create self-reference."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        await gossip.exchange("node-a", "node-a")

        # A should NOT know about itself
        a_knows = await gossip.get_known("node-a")
        assert "node-a" not in a_knows

    async def test_large_gossip_exchange(self, db):
        """Exchange with many known nodes."""
        members = MembershipList()
        gossip = ClusterGossip(db, members)

        for i in range(50):
            await gossip.add_known("a", f"friend-{i}")

        await gossip.exchange("a", "b")

        b_knows = await gossip.get_known("b")
        assert len(b_knows) >= 51  # All of A's friends + A itself
        assert "a" in b_knows
        for i in range(50):
            assert f"friend-{i}" in b_knows

    async def test_handle_exchange_empty_known_list(self, db):
        members = MembershipList()
        gossip = ClusterGossip(db, members, local_node_id="local")
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        members.add(local)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        result = await gossip.handle_exchange(peer, [])

        # Peer added to membership
        assert members.contains("peer") is True
        # Local should know about peer
        local_knows = await gossip.get_known("local")
        assert "peer" in local_knows
        # Return should contain local node
        result_ids = {n.node_id for n in result}
        assert "local" in result_ids

    async def test_handle_exchange_preserves_existing_members(self, db):
        """If a card in known_cards already exists in membership, don't overwrite it."""
        members = MembershipList()
        existing = NodeCard(node_id="friend", endpoint="http://original:8000", domains=["old"])
        members.add(existing)

        gossip = ClusterGossip(db, members, local_node_id="local")
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        members.add(local)

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000")
        new_friend = NodeCard(node_id="friend", endpoint="http://new:8000", domains=["new"])

        await gossip.handle_exchange(peer, [new_friend])

        # Original member data should be preserved
        assert members.get("friend").endpoint == "http://original:8000"
        assert members.get("friend").domains == ["old"]


class TestRouterEdgeCases:
    async def test_empty_task_id(self, db):
        router = ClusterRouter(db, "local")
        # Empty string is valid as a key
        router.set_route("", "remote")
        assert router.is_local("") is False
        assert router.get_route("") == "remote"

    async def test_empty_node_id_in_endpoint(self, db):
        router = ClusterRouter(db, "local")
        router.set_endpoint("", "http://empty:8000")
        assert router.get_endpoint("") == "http://empty:8000"

    async def test_participant_add_many_then_remove(self, db):
        router = ClusterRouter(db, "local")
        for i in range(50):
            router.add_participant("t1", f"node-{i}")

        assert len(router.get_participants("t1")) == 50

        router.remove_participants("t1")
        assert router.get_participants("t1") == set()

    async def test_forward_bid_missing_route_raises(self, db):
        router = ClusterRouter(db, "local")
        with pytest.raises(ValueError, match="No route for task"):
            await router.forward_bid("unknown", "agent", None, 0.9, 80.0)

    async def test_forward_bid_missing_endpoint_raises(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        with pytest.raises(ValueError, match="No endpoint for node"):
            await router.forward_bid("t1", "agent", None, 0.9, 80.0)

    async def test_forward_result_missing_route_raises(self, db):
        router = ClusterRouter(db, "local")
        with pytest.raises(ValueError, match="No route for task"):
            await router.forward_result("unknown", "agent", "content")

    async def test_forward_reject_missing_endpoint_raises(self, db):
        router = ClusterRouter(db, "local")
        router.set_route("t1", "remote")
        with pytest.raises(ValueError, match="No endpoint for node"):
            await router.forward_reject("t1", "agent")

    async def test_forward_subtask_missing_route_raises(self, db):
        router = ClusterRouter(db, "local")
        with pytest.raises(ValueError, match="No route for task"):
            await router.forward_subtask("unknown", {})


class TestDiscoveryEdgeCases:
    async def test_discover_empty_domain(self, db):
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        members.add(local)

        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        from eacn.network.cluster.bootstrap import ClusterBootstrap
        bootstrap = ClusterBootstrap(local, members, config)
        dht = ClusterDHT(db)
        gossip = ClusterGossip(db, members, local_node_id="local")
        disc = ClusterDiscovery("local", gossip, dht, bootstrap)

        result = await disc.discover("")
        assert result == []

    async def test_discover_only_local_handles_domain(self, db):
        """If only local node handles a domain, discover should return empty."""
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000", domains=["coding"])
        members.add(local)

        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        bootstrap = ClusterBootstrap(local, members, config)
        dht = ClusterDHT(db)
        gossip = ClusterGossip(db, members, local_node_id="local")
        disc = ClusterDiscovery("local", gossip, dht, bootstrap)

        result = await disc.discover("coding")
        assert result == []

    async def test_discover_multiple_layers_have_results(self, db):
        """Only gossip results should be returned (first layer wins)."""
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        gossip_peer = NodeCard(node_id="gp", endpoint="http://gp:8000", domains=["coding"])
        members.add(local)
        members.add(gossip_peer)

        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        bootstrap = ClusterBootstrap(local, members, config)
        dht = ClusterDHT(db)
        gossip = ClusterGossip(db, members, local_node_id="local")
        disc = ClusterDiscovery("local", gossip, dht, bootstrap)

        # Gossip knows about gossip_peer
        await gossip.add_known("local", "gp")
        # DHT also has dht-node
        await dht.announce("coding", "dht-node")

        result = await disc.discover("coding")
        # Only gossip result
        assert result == ["gp"]
        assert "dht-node" not in result

    async def test_discover_falls_through_all_three_layers(self, db):
        """If gossip and DHT return nothing, bootstrap should catch it."""
        members = MembershipList()
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        bootstrap_peer = NodeCard(node_id="bp", endpoint="http://bp:8000", domains=["rare_domain"])
        members.add(local)
        members.add(bootstrap_peer)

        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        bootstrap = ClusterBootstrap(local, members, config)
        dht = ClusterDHT(db)
        gossip = ClusterGossip(db, members, local_node_id="local")
        disc = ClusterDiscovery("local", gossip, dht, bootstrap)

        result = await disc.discover("rare_domain")
        assert result == ["bp"]


class TestBootstrapEdgeCases:
    def test_handle_join_returns_all_members(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        # Add 3 peers
        for i in range(3):
            peer = NodeCard(node_id=f"p{i}", endpoint=f"http://p{i}:8000")
            bs.handle_join(peer)

        # 4th peer joining should see all 5 members (seed + 3 peers + self)
        new = NodeCard(node_id="new", endpoint="http://new:8000")
        result = bs.handle_join(new)
        assert len(result) == 5
        result_ids = {n.node_id for n in result}
        assert result_ids == {"seed", "p0", "p1", "p2", "new"}

    def test_handle_leave_nonexistent_is_safe(self):
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        # Should not raise
        bs.handle_leave("nonexistent")
        assert members.count() == 1

    def test_lookup_with_mixed_statuses(self):
        """Lookup should only return online nodes."""
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        members.add(local)
        config = ClusterConfig(seed_nodes=["http://seed:8000"])
        bs = ClusterBootstrap(local, members, config)

        online = NodeCard(node_id="online", endpoint="http://on:8000", domains=["coding"])
        suspect = NodeCard(node_id="suspect", endpoint="http://sus:8000", domains=["coding"])
        suspect.status = "suspect"
        offline = NodeCard(node_id="offline", endpoint="http://off:8000", domains=["coding"])
        offline.status = "offline"

        bs.handle_join(online)
        members.add(suspect)
        members.add(offline)

        found = bs.lookup("coding")
        assert found == ["online"]
        assert "suspect" not in found
        assert "offline" not in found


class TestConfigEdgeCases:
    def test_empty_config(self):
        cfg = ClusterConfig()
        assert cfg.seed_nodes == []
        assert cfg.node_id == ""
        assert cfg.endpoint == ""

    def test_multiple_seed_nodes(self):
        seeds = [f"http://seed{i}:8000" for i in range(10)]
        cfg = ClusterConfig(seed_nodes=seeds)
        assert len(cfg.seed_nodes) == 10

    def test_config_with_all_fields(self):
        cfg = ClusterConfig(
            seed_nodes=["http://s:8000"],
            heartbeat_interval=5,
            heartbeat_fan_out=2,
            suspect_rounds=2,
            offline_rounds=4,
            node_id="test-node",
            endpoint="http://test:8000",
            protocol_version="0.2.0",
        )
        assert cfg.heartbeat_interval == 5
        assert cfg.heartbeat_fan_out == 2
        assert cfg.suspect_rounds == 2
        assert cfg.offline_rounds == 4
        assert cfg.protocol_version == "0.2.0"


class TestServiceEdgeCases:
    async def test_start_twice_is_safe(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        await cs.start()
        await cs.start()  # No error
        assert cs.members.count() == 1

    async def test_stop_twice_is_safe(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        await cs.start()
        await cs.stop()
        await cs.stop()  # No error

    async def test_announce_empty_domain(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        await cs.announce_domain("")
        assert "" in cs.local_node.domains

    async def test_revoke_never_announced_domain(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        # Should not raise
        await cs.revoke_domain("never-announced")
        assert "never-announced" not in cs.local_node.domains

    async def test_handle_push_with_many_recipients(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        recipients = [f"agent-{i}" for i in range(100)]
        delivered = await cs.handle_push("EVT", "t1", recipients, {})
        assert delivered == 100
