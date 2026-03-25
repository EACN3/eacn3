"""Database concurrent access tests.

Tests that the _write_lock properly serializes operations:
- Concurrent account upserts
- Concurrent escrow operations
- Concurrent offline store writes
- Mixed read/write under load
"""

import asyncio
import pytest
from eacn3.network.db.database import Database


@pytest.fixture
async def db():
    d = Database()
    await d.connect()
    yield d
    await d.close()


class TestConcurrentAccountWrites:
    @pytest.mark.asyncio
    async def test_50_concurrent_account_upserts(self, db):
        """50 concurrent upserts to the same account — no crashes."""
        async def upsert(i: int):
            await db.upsert_account("agent-x", float(i), 0.0)

        await asyncio.gather(*[upsert(i) for i in range(50)])
        acct = await db.get_account("agent-x")
        assert acct is not None
        # Final value should be one of the upserted values
        assert 0.0 <= acct["available"] <= 49.0

    @pytest.mark.asyncio
    async def test_concurrent_different_accounts(self, db):
        """10 different accounts upserted concurrently."""
        await asyncio.gather(*[
            db.upsert_account(f"agent-{i}", float(i * 100), 0.0)
            for i in range(10)
        ])
        for i in range(10):
            acct = await db.get_account(f"agent-{i}")
            assert acct is not None
            assert acct["available"] == float(i * 100)


class TestConcurrentEscrowWrites:
    @pytest.mark.asyncio
    async def test_concurrent_escrow_saves(self, db):
        """20 concurrent escrow saves — all succeed."""
        await asyncio.gather(*[
            db.save_escrow(f"task-{i}", "init-1", float(i * 10))
            for i in range(20)
        ])
        escrows = await db.list_all_escrows()
        assert len(escrows) == 20

    @pytest.mark.asyncio
    async def test_concurrent_escrow_save_and_delete(self, db):
        """Save and delete escrows concurrently."""
        # First create some escrows
        for i in range(10):
            await db.save_escrow(f"del-{i}", "init-1", 100.0)

        # Concurrently save new ones and delete old ones
        saves = [db.save_escrow(f"new-{i}", "init-1", 200.0) for i in range(5)]
        deletes = [db.delete_escrow(f"del-{i}") for i in range(5)]
        await asyncio.gather(*saves, *deletes)

        escrows = await db.list_all_escrows()
        # Should have: 5 remaining old + 5 new = 10
        assert len(escrows) == 10


class TestConcurrentOfflineStore:
    @pytest.mark.asyncio
    async def test_20_concurrent_stores(self, db):
        """20 concurrent offline_store writes to different agents."""
        await asyncio.gather(*[
            db.offline_store(f"msg-{i}", f"agent-{i % 5}", "broadcast", "t1", {"i": i})
            for i in range(20)
        ])
        counts = await db.offline_count_all()
        total = sum(counts.values())
        assert total == 20

    @pytest.mark.asyncio
    async def test_concurrent_store_and_drain_different_agents(self, db):
        """Store to agent-a while draining agent-b."""
        for i in range(5):
            await db.offline_store(f"pre-{i}", "drain-agent", "broadcast", "t1", {})

        async def store_to_a():
            for i in range(10):
                await db.offline_store(f"a-{i}", "store-agent", "broadcast", "t1", {})

        async def drain_b():
            return await db.offline_drain("drain-agent")

        _, msgs = await asyncio.gather(store_to_a(), drain_b())
        assert len(msgs) == 5  # drain-agent had 5 messages

        # store-agent should have 10
        count = await db.offline_count("store-agent")
        assert count == 10


class TestConcurrentDHT:
    @pytest.mark.asyncio
    async def test_concurrent_dht_announces(self, db):
        """30 concurrent DHT announces."""
        await asyncio.gather(*[
            db.dht_announce(f"domain-{i % 5}", f"agent-{i}")
            for i in range(30)
        ])
        for d in range(5):
            agents = await db.dht_lookup(f"domain-{d}")
            assert len(agents) == 6  # 30/5 = 6 agents per domain

    @pytest.mark.asyncio
    async def test_concurrent_announce_and_revoke(self, db):
        """Announce and revoke simultaneously."""
        for i in range(10):
            await db.dht_announce("test-domain", f"agent-{i}")

        announces = [db.dht_announce("test-domain", f"new-{i}") for i in range(5)]
        revokes = [db.dht_revoke("test-domain", f"agent-{i}") for i in range(5)]
        await asyncio.gather(*announces, *revokes)

        agents = await db.dht_lookup("test-domain")
        # 10 - 5 revoked + 5 new = 10
        assert len(agents) == 10


class TestConcurrentReputation:
    @pytest.mark.asyncio
    async def test_concurrent_reputation_upserts(self, db):
        """15 concurrent reputation updates."""
        await asyncio.gather(*[
            db.upsert_reputation(f"rep-{i}", 0.5 + i * 0.01, {})
            for i in range(15)
        ])
        reps = await db.list_all_reputations()
        assert len(reps) == 15


class TestMixedReadWrite:
    @pytest.mark.asyncio
    async def test_reads_during_writes(self, db):
        """Reads and writes happening concurrently — no crashes."""
        async def writer():
            for i in range(20):
                await db.upsert_account(f"rw-{i}", float(i), 0.0)

        async def reader():
            results = []
            for i in range(20):
                acct = await db.get_account(f"rw-{i}")
                results.append(acct)
            return results

        await asyncio.gather(writer(), reader())
        # No crash is the assertion — reads may return None or data
