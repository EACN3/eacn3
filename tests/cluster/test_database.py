"""Tests for cluster database methods."""

import pytest


class TestClusterNodeStore:
    async def test_save_and_get_node(self, db):
        node = {
            "node_id": "n1",
            "endpoint": "http://n1:8000",
            "domains": ["coding"],
            "status": "online",
            "version": "0.1.0",
            "joined_at": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
        }
        await db.cluster_save_node(node)
        result = await db.cluster_get_node("n1")
        assert result is not None
        assert result["node_id"] == "n1"
        assert result["endpoint"] == "http://n1:8000"
        assert result["domains"] == ["coding"]
        assert result["status"] == "online"

    async def test_save_node_upsert(self, db):
        node = {
            "node_id": "n1", "endpoint": "http://n1:8000",
            "domains": [], "status": "online", "version": "0.1.0",
            "joined_at": "", "last_seen": "",
        }
        await db.cluster_save_node(node)
        node["endpoint"] = "http://n1-new:8000"
        await db.cluster_save_node(node)
        result = await db.cluster_get_node("n1")
        assert result["endpoint"] == "http://n1-new:8000"

    async def test_get_node_nonexistent(self, db):
        result = await db.cluster_get_node("nonexistent")
        assert result is None

    async def test_get_all_nodes(self, db):
        for i in range(3):
            await db.cluster_save_node({
                "node_id": f"n{i}", "endpoint": f"http://n{i}:8000",
                "domains": [], "status": "online", "version": "0.1.0",
                "joined_at": "", "last_seen": "",
            })
        nodes = await db.cluster_get_all_nodes()
        assert len(nodes) == 3

    async def test_remove_node(self, db):
        await db.cluster_save_node({
            "node_id": "n1", "endpoint": "http://n1:8000",
            "domains": [], "status": "online", "version": "0.1.0",
            "joined_at": "", "last_seen": "",
        })
        await db.cluster_remove_node("n1")
        assert await db.cluster_get_node("n1") is None

    async def test_update_node_status(self, db):
        await db.cluster_save_node({
            "node_id": "n1", "endpoint": "http://n1:8000",
            "domains": [], "status": "online", "version": "0.1.0",
            "joined_at": "", "last_seen": "",
        })
        await db.cluster_update_node_status("n1", "suspect")
        result = await db.cluster_get_node("n1")
        assert result["status"] == "suspect"


class TestClusterTaskRoutes:
    async def test_set_and_get_route(self, db):
        await db.cluster_set_route("t1", "origin-1")
        result = await db.cluster_get_route("t1")
        assert result == "origin-1"

    async def test_get_route_nonexistent(self, db):
        result = await db.cluster_get_route("nonexistent")
        assert result is None

    async def test_set_route_upsert(self, db):
        await db.cluster_set_route("t1", "origin-1")
        await db.cluster_set_route("t1", "origin-2")
        result = await db.cluster_get_route("t1")
        assert result == "origin-2"

    async def test_remove_route(self, db):
        await db.cluster_set_route("t1", "origin-1")
        await db.cluster_remove_route("t1")
        assert await db.cluster_get_route("t1") is None


class TestClusterTaskParticipants:
    async def test_add_and_get(self, db):
        await db.cluster_add_participant("t1", "n1")
        await db.cluster_add_participant("t1", "n2")
        result = await db.cluster_get_participants("t1")
        assert result == {"n1", "n2"}

    async def test_add_idempotent(self, db):
        await db.cluster_add_participant("t1", "n1")
        await db.cluster_add_participant("t1", "n1")
        result = await db.cluster_get_participants("t1")
        assert result == {"n1"}

    async def test_get_empty(self, db):
        result = await db.cluster_get_participants("nonexistent")
        assert result == set()

    async def test_remove(self, db):
        await db.cluster_add_participant("t1", "n1")
        await db.cluster_remove_participants("t1")
        assert await db.cluster_get_participants("t1") == set()


class TestClusterDHTStore:
    async def test_store_and_lookup(self, db):
        await db.cluster_dht_store("coding", "n1")
        result = await db.cluster_dht_lookup("coding")
        assert result == ["n1"]

    async def test_store_multiple(self, db):
        await db.cluster_dht_store("coding", "n1")
        await db.cluster_dht_store("coding", "n2")
        result = await db.cluster_dht_lookup("coding")
        assert set(result) == {"n1", "n2"}

    async def test_store_idempotent(self, db):
        await db.cluster_dht_store("coding", "n1")
        await db.cluster_dht_store("coding", "n1")
        result = await db.cluster_dht_lookup("coding")
        assert result == ["n1"]

    async def test_revoke(self, db):
        await db.cluster_dht_store("coding", "n1")
        await db.cluster_dht_revoke("coding", "n1")
        result = await db.cluster_dht_lookup("coding")
        assert result == []

    async def test_revoke_all(self, db):
        await db.cluster_dht_store("coding", "n1")
        await db.cluster_dht_store("design", "n1")
        await db.cluster_dht_revoke_all("n1")
        assert await db.cluster_dht_lookup("coding") == []
        assert await db.cluster_dht_lookup("design") == []

    async def test_lookup_empty(self, db):
        result = await db.cluster_dht_lookup("nonexistent")
        assert result == []


class TestClusterGossipStore:
    async def test_add_and_get(self, db):
        await db.cluster_gossip_add("n1", "n2")
        result = await db.cluster_gossip_get_known("n1")
        assert result == {"n2"}

    async def test_add_many(self, db):
        await db.cluster_gossip_add_many("n1", {"n2", "n3", "n4"})
        result = await db.cluster_gossip_get_known("n1")
        assert result == {"n2", "n3", "n4"}

    async def test_add_idempotent(self, db):
        await db.cluster_gossip_add("n1", "n2")
        await db.cluster_gossip_add("n1", "n2")
        result = await db.cluster_gossip_get_known("n1")
        assert result == {"n2"}

    async def test_get_empty(self, db):
        result = await db.cluster_gossip_get_known("nonexistent")
        assert result == set()

    async def test_remove(self, db):
        await db.cluster_gossip_add("n1", "n2")
        await db.cluster_gossip_add("n2", "n1")
        await db.cluster_gossip_remove("n2")
        # n2 removed from n1's list and n2's own list cleared
        assert await db.cluster_gossip_get_known("n1") == set()
        assert await db.cluster_gossip_get_known("n2") == set()
