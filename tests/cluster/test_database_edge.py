"""Tests for database edge cases: unicode, large data, cross-table consistency,
sequential operations, and boundary conditions.
"""

import pytest


class TestNodeStoreEdgeCases:
    async def test_unicode_node_id(self, db):
        node = {
            "node_id": "节点-1",
            "endpoint": "http://n1:8000",
            "domains": ["编程"],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        }
        await db.cluster_save_node(node)
        result = await db.cluster_get_node("节点-1")
        assert result is not None
        assert result["node_id"] == "节点-1"
        assert result["domains"] == ["编程"]

    async def test_node_with_many_domains(self, db):
        domains = [f"domain-{i}" for i in range(100)]
        node = {
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "domains": domains,
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        }
        await db.cluster_save_node(node)
        result = await db.cluster_get_node("n1")
        assert len(result["domains"]) == 100

    async def test_node_with_empty_domains(self, db):
        node = {
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "domains": [],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        }
        await db.cluster_save_node(node)
        result = await db.cluster_get_node("n1")
        assert result["domains"] == []

    async def test_save_many_nodes(self, db):
        for i in range(200):
            await db.cluster_save_node({
                "node_id": f"n{i}",
                "endpoint": f"http://n{i}:8000",
                "domains": ["coding"],
                "status": "online",
                "version": "0.1.0",
                "joined_at": "",
                "last_seen": "",
            })
        all_nodes = await db.cluster_get_all_nodes()
        assert len(all_nodes) == 200

    async def test_remove_nonexistent_node(self, db):
        """Removing nonexistent node should not error."""
        await db.cluster_remove_node("nonexistent")
        assert await db.cluster_get_node("nonexistent") is None

    async def test_update_status_nonexistent_node(self, db):
        """Updating status of nonexistent node should not error."""
        await db.cluster_update_node_status("nonexistent", "offline")
        assert await db.cluster_get_node("nonexistent") is None

    async def test_save_update_remove_cycle(self, db):
        node = {
            "node_id": "cycle",
            "endpoint": "http://c:8000",
            "domains": [],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        }
        await db.cluster_save_node(node)
        assert (await db.cluster_get_node("cycle"))["status"] == "online"

        await db.cluster_update_node_status("cycle", "suspect")
        assert (await db.cluster_get_node("cycle"))["status"] == "suspect"

        await db.cluster_update_node_status("cycle", "offline")
        assert (await db.cluster_get_node("cycle"))["status"] == "offline"

        await db.cluster_remove_node("cycle")
        assert await db.cluster_get_node("cycle") is None


class TestRouteStoreEdgeCases:
    async def test_many_routes(self, db):
        for i in range(200):
            await db.cluster_set_route(f"t{i}", f"origin-{i % 10}")

        for i in range(200):
            result = await db.cluster_get_route(f"t{i}")
            assert result == f"origin-{i % 10}"

    async def test_remove_nonexistent_route(self, db):
        await db.cluster_remove_route("nonexistent")
        assert await db.cluster_get_route("nonexistent") is None

    async def test_overwrite_route_multiple_times(self, db):
        for i in range(10):
            await db.cluster_set_route("t1", f"origin-{i}")
        result = await db.cluster_get_route("t1")
        assert result == "origin-9"

    async def test_unicode_task_and_origin(self, db):
        await db.cluster_set_route("任务-1", "节点-A")
        result = await db.cluster_get_route("任务-1")
        assert result == "节点-A"


class TestParticipantStoreEdgeCases:
    async def test_many_participants(self, db):
        for i in range(100):
            await db.cluster_add_participant("t1", f"n{i}")
        result = await db.cluster_get_participants("t1")
        assert len(result) == 100
        assert result == {f"n{i}" for i in range(100)}

    async def test_participants_isolated_between_tasks(self, db):
        await db.cluster_add_participant("t1", "n1")
        await db.cluster_add_participant("t2", "n2")
        await db.cluster_add_participant("t3", "n3")

        assert await db.cluster_get_participants("t1") == {"n1"}
        assert await db.cluster_get_participants("t2") == {"n2"}
        assert await db.cluster_get_participants("t3") == {"n3"}

    async def test_remove_participants_only_affects_target(self, db):
        await db.cluster_add_participant("t1", "n1")
        await db.cluster_add_participant("t2", "n1")

        await db.cluster_remove_participants("t1")
        assert await db.cluster_get_participants("t1") == set()
        assert await db.cluster_get_participants("t2") == {"n1"}

    async def test_unicode_participant_ids(self, db):
        await db.cluster_add_participant("任务", "节点")
        result = await db.cluster_get_participants("任务")
        assert result == {"节点"}


class TestDHTStoreEdgeCases:
    async def test_many_domains(self, db):
        for i in range(100):
            await db.cluster_dht_store(f"domain-{i}", "n1")

        for i in range(100):
            result = await db.cluster_dht_lookup(f"domain-{i}")
            assert result == ["n1"]

    async def test_revoke_nonexistent_is_safe(self, db):
        await db.cluster_dht_revoke("nonexistent", "nonexistent")
        result = await db.cluster_dht_lookup("nonexistent")
        assert result == []

    async def test_revoke_all_nonexistent_is_safe(self, db):
        await db.cluster_dht_revoke_all("nonexistent")

    async def test_unicode_domain_and_node(self, db):
        await db.cluster_dht_store("编程", "节点-1")
        result = await db.cluster_dht_lookup("编程")
        assert result == ["节点-1"]

    async def test_store_revoke_cycle(self, db):
        for _ in range(5):
            await db.cluster_dht_store("cycling", "n1")
            assert await db.cluster_dht_lookup("cycling") == ["n1"]
            await db.cluster_dht_revoke("cycling", "n1")
            assert await db.cluster_dht_lookup("cycling") == []

    async def test_case_sensitive_domains_in_db(self, db):
        await db.cluster_dht_store("Coding", "n1")
        await db.cluster_dht_store("coding", "n2")
        assert await db.cluster_dht_lookup("Coding") == ["n1"]
        assert await db.cluster_dht_lookup("coding") == ["n2"]


class TestGossipStoreEdgeCases:
    async def test_many_known_nodes(self, db):
        known = {f"n{i}" for i in range(100)}
        await db.cluster_gossip_add_many("local", known)
        result = await db.cluster_gossip_get_known("local")
        assert result == known

    async def test_add_many_empty_set(self, db):
        await db.cluster_gossip_add_many("local", set())
        result = await db.cluster_gossip_get_known("local")
        assert result == set()

    async def test_remove_cleans_bidirectional(self, db):
        await db.cluster_gossip_add("a", "b")
        await db.cluster_gossip_add("a", "c")
        await db.cluster_gossip_add("b", "a")
        await db.cluster_gossip_add("c", "a")

        await db.cluster_gossip_remove("a")

        # a's own list cleared
        assert await db.cluster_gossip_get_known("a") == set()
        # a removed from others' lists
        assert "a" not in await db.cluster_gossip_get_known("b")
        assert "a" not in await db.cluster_gossip_get_known("c")
        # b and c still know each other? No — they didn't add each other
        assert await db.cluster_gossip_get_known("b") == set()
        assert await db.cluster_gossip_get_known("c") == set()

    async def test_unicode_gossip(self, db):
        await db.cluster_gossip_add("节点-A", "节点-B")
        result = await db.cluster_gossip_get_known("节点-A")
        assert result == {"节点-B"}

    async def test_add_then_remove_then_add(self, db):
        await db.cluster_gossip_add("a", "b")
        assert await db.cluster_gossip_get_known("a") == {"b"}

        await db.cluster_gossip_remove("b")
        assert await db.cluster_gossip_get_known("a") == set()

        await db.cluster_gossip_add("a", "b")
        assert await db.cluster_gossip_get_known("a") == {"b"}

    async def test_add_many_overlapping_with_existing(self, db):
        await db.cluster_gossip_add("a", "b")
        await db.cluster_gossip_add("a", "c")
        # Add many including existing
        await db.cluster_gossip_add_many("a", {"b", "c", "d", "e"})
        result = await db.cluster_gossip_get_known("a")
        assert result == {"b", "c", "d", "e"}


class TestCrossTableConsistency:
    async def test_node_save_and_route_independent(self, db):
        """Node store and route store are independent."""
        await db.cluster_save_node({
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "domains": [],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        })
        await db.cluster_set_route("t1", "n1")

        # Remove node — route should persist
        await db.cluster_remove_node("n1")
        assert await db.cluster_get_node("n1") is None
        assert await db.cluster_get_route("t1") == "n1"  # Orphaned route

    async def test_node_dht_independent(self, db):
        """Node store and DHT are independent."""
        await db.cluster_save_node({
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "domains": ["coding"],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "",
            "last_seen": "",
        })
        await db.cluster_dht_store("coding", "n1")

        # Remove node — DHT should persist
        await db.cluster_remove_node("n1")
        assert await db.cluster_get_node("n1") is None
        assert await db.cluster_dht_lookup("coding") == ["n1"]  # Orphaned DHT entry

    async def test_gossip_dht_independent(self, db):
        """Gossip and DHT are independent stores."""
        await db.cluster_gossip_add("n1", "n2")
        await db.cluster_dht_store("coding", "n2")

        await db.cluster_gossip_remove("n2")
        # DHT unaffected
        assert await db.cluster_dht_lookup("coding") == ["n2"]

    async def test_route_participant_independent(self, db):
        """Routes and participants are independent."""
        await db.cluster_set_route("t1", "origin")
        await db.cluster_add_participant("t1", "p1")
        await db.cluster_add_participant("t1", "p2")

        await db.cluster_remove_route("t1")
        # Participants still exist
        assert await db.cluster_get_participants("t1") == {"p1", "p2"}
