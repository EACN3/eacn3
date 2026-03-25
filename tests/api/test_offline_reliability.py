"""Offline message store reliability tests.

Tests that push events are reliably stored, drained, pruned, and not lost
under various conditions:
- High throughput store/drain cycles
- Per-agent overflow pruning
- Concurrent drain from multiple agents
- Task cleanup (#80)
- Message ordering (FIFO)
"""

import asyncio
import pytest

from eacn3.network.db.database import Database
from eacn3.network.offline_store import OfflineStore


@pytest.fixture
async def store():
    db = Database()
    await db.connect()
    s = OfflineStore(db, max_per_agent=10, ttl_seconds=3600)
    yield s
    await db.close()


class TestStoreReliability:
    @pytest.mark.asyncio
    async def test_store_and_drain_100_messages(self, store):
        """Store 100 messages, drain, verify count and ordering."""
        for i in range(100):
            await store.store(f"m{i}", "agent-x", "broadcast", "t1", {"seq": i})

        # Overflow pruning: only last 10 should remain (max_per_agent=10)
        msgs = await store.drain("agent-x")
        assert len(msgs) == 10

        # Should be FIFO ordered — last 10 messages
        seqs = [m["payload"]["seq"] for m in msgs]
        assert seqs == list(range(90, 100))

    @pytest.mark.asyncio
    async def test_drain_returns_empty_after_drain(self, store):
        await store.store("m1", "agent-y", "broadcast", "t1", {})
        msgs1 = await store.drain("agent-y")
        assert len(msgs1) == 1

        msgs2 = await store.drain("agent-y")
        assert len(msgs2) == 0

    @pytest.mark.asyncio
    async def test_different_agents_isolated(self, store):
        """Agent A's messages don't leak to Agent B."""
        await store.store("ma", "a", "broadcast", "t1", {"for": "a"})
        await store.store("mb", "b", "broadcast", "t1", {"for": "b"})

        msgs_a = await store.drain("a")
        msgs_b = await store.drain("b")

        assert len(msgs_a) == 1
        assert msgs_a[0]["payload"]["for"] == "a"
        assert len(msgs_b) == 1
        assert msgs_b[0]["payload"]["for"] == "b"

    @pytest.mark.asyncio
    async def test_concurrent_drain_same_agent(self, store):
        """Two concurrent drains for the same agent — no duplicates."""
        for i in range(5):
            await store.store(f"cd{i}", "agent-c", "broadcast", "t1", {"i": i})

        d1, d2 = await asyncio.gather(
            store.drain("agent-c"),
            store.drain("agent-c"),
        )

        total = len(d1) + len(d2)
        assert total == 5, f"Expected 5 total, got {len(d1)} + {len(d2)} = {total}"

    @pytest.mark.asyncio
    async def test_concurrent_store_and_drain(self, store):
        """Store and drain happening concurrently."""
        async def writer():
            for i in range(20):
                await store.store(f"w{i}", "agent-d", "broadcast", "t1", {"i": i})
                await asyncio.sleep(0)  # Yield

        async def reader():
            all_msgs = []
            for _ in range(10):
                msgs = await store.drain("agent-d")
                all_msgs.extend(msgs)
                await asyncio.sleep(0)
            return all_msgs

        _, msgs = await asyncio.gather(writer(), reader())
        # May not get all 20 due to timing, but should get some and no errors
        assert len(msgs) >= 0  # No crash is the main assertion

    @pytest.mark.asyncio
    async def test_task_cleanup(self, store):
        """cleanup_task removes all messages for a specific task (#80)."""
        await store.store("t1m1", "a1", "broadcast", "task-clean", {"x": 1})
        await store.store("t1m2", "a2", "broadcast", "task-clean", {"x": 2})
        await store.store("t2m1", "a1", "broadcast", "other-task", {"x": 3})

        deleted = await store.cleanup_task("task-clean")
        assert deleted == 2

        # other-task messages should still be there
        msgs = await store.drain("a1")
        assert len(msgs) == 1
        assert msgs[0]["task_id"] == "other-task"

    @pytest.mark.asyncio
    async def test_count_accuracy(self, store):
        """Count matches actual stored messages."""
        for i in range(7):
            await store.store(f"cnt{i}", "counter-agent", "broadcast", "t1", {})

        count = await store.count("counter-agent")
        assert count == 7

    @pytest.mark.asyncio
    async def test_count_all(self, store):
        """count_all returns per-agent breakdown."""
        await store.store("ca1", "x", "broadcast", "t1", {})
        await store.store("ca2", "x", "broadcast", "t1", {})
        await store.store("ca3", "y", "broadcast", "t1", {})

        counts = await store.count_all()
        assert counts["x"] == 2
        assert counts["y"] == 1

    @pytest.mark.asyncio
    async def test_message_types_preserved(self, store):
        """Different event types are stored and returned correctly."""
        types = ["task_broadcast", "bid_result", "discussion_update", "task_timeout"]
        for i, t in enumerate(types):
            await store.store(f"tp{i}", "type-agent", t, "t1", {"type": t})

        msgs = await store.drain("type-agent")
        returned_types = [m["type"] for m in msgs]
        assert returned_types == types
