"""Tests for ClusterDHT."""

import pytest
from eacn.network.cluster.dht import ClusterDHT


class TestClusterDHT:
    async def test_announce_and_lookup(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        result = await dht.lookup("coding")
        assert result == ["node-1"]

    async def test_announce_multiple_nodes(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        await dht.announce("coding", "node-2")
        result = await dht.lookup("coding")
        assert set(result) == {"node-1", "node-2"}

    async def test_announce_idempotent(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        await dht.announce("coding", "node-1")
        result = await dht.lookup("coding")
        assert result == ["node-1"]

    async def test_revoke(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        await dht.revoke("coding", "node-1")
        result = await dht.lookup("coding")
        assert result == []

    async def test_revoke_nonexistent_silent(self, db):
        dht = ClusterDHT(db)
        await dht.revoke("coding", "nonexistent")  # No error

    async def test_revoke_all(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        await dht.announce("design", "node-1")
        await dht.announce("coding", "node-2")
        await dht.revoke_all("node-1")
        assert await dht.lookup("coding") == ["node-2"]
        assert await dht.lookup("design") == []

    async def test_lookup_empty_domain(self, db):
        dht = ClusterDHT(db)
        result = await dht.lookup("nonexistent")
        assert result == []

    async def test_handle_store_and_lookup(self, db):
        dht = ClusterDHT(db)
        await dht.handle_store("coding", "node-1")
        result = await dht.handle_lookup("coding")
        assert result == ["node-1"]

    async def test_handle_revoke(self, db):
        dht = ClusterDHT(db)
        await dht.handle_store("coding", "node-1")
        await dht.handle_revoke("coding", "node-1")
        result = await dht.handle_lookup("coding")
        assert result == []

    async def test_multiple_domains_same_node(self, db):
        dht = ClusterDHT(db)
        await dht.announce("coding", "node-1")
        await dht.announce("design", "node-1")
        await dht.announce("research", "node-1")
        assert await dht.lookup("coding") == ["node-1"]
        assert await dht.lookup("design") == ["node-1"]
        assert await dht.lookup("research") == ["node-1"]
