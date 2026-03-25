"""Tests: Offline message queue (OfflineStore) and PushEvent msg_id.

Covers:
- OfflineStore: store, drain, count, prune
- PushEvent: msg_id generation
"""

import pytest

from eacn3.core.models import PushEvent, PushEventType
from eacn3.network.db.database import Database
from eacn3.network.offline_store import OfflineStore


# ── OfflineStore unit tests ──────────────────────────────────────────

class TestOfflineStore:
    @pytest.fixture
    async def store(self):
        db = Database()
        await db.connect()
        s = OfflineStore(db, max_per_agent=5, ttl_seconds=3600)
        yield s
        await db.close()

    @pytest.mark.asyncio
    async def test_store_and_drain(self, store):
        await store.store("m1", "a1", "task_broadcast", "t1", {"k": "v"})
        await store.store("m2", "a1", "bid_result", "t1", {"k2": "v2"})

        messages = await store.drain("a1")
        assert len(messages) == 2
        assert messages[0]["msg_id"] == "m1"
        assert messages[1]["msg_id"] == "m2"
        assert messages[0]["payload"] == {"k": "v"}

        # After drain, should be empty
        messages2 = await store.drain("a1")
        assert len(messages2) == 0

    @pytest.mark.asyncio
    async def test_drain_empty(self, store):
        messages = await store.drain("nobody")
        assert messages == []

    @pytest.mark.asyncio
    async def test_count(self, store):
        assert await store.count("a1") == 0
        await store.store("m1", "a1", "task_broadcast", "t1", {})
        await store.store("m2", "a1", "bid_result", "t2", {})
        assert await store.count("a1") == 2

    @pytest.mark.asyncio
    async def test_count_all(self, store):
        await store.store("m1", "a1", "task_broadcast", "t1", {})
        await store.store("m2", "a2", "bid_result", "t1", {})
        await store.store("m3", "a1", "bid_result", "t2", {})
        counts = await store.count_all()
        assert counts == {"a1": 2, "a2": 1}

    @pytest.mark.asyncio
    async def test_prune_overflow(self, store):
        """When max_per_agent=5, storing 7 messages should prune 2 oldest."""
        for i in range(7):
            await store.store(f"m{i}", "a1", "task_broadcast", "t1", {"i": i})
        assert await store.count("a1") == 5
        messages = await store.drain("a1")
        # Oldest 2 (m0, m1) should be gone; m2..m6 remain
        assert [m["msg_id"] for m in messages] == ["m2", "m3", "m4", "m5", "m6"]

    @pytest.mark.asyncio
    async def test_separate_agents(self, store):
        """Messages for different agents don't interfere."""
        await store.store("m1", "a1", "task_broadcast", "t1", {})
        await store.store("m2", "a2", "bid_result", "t1", {})

        a1_msgs = await store.drain("a1")
        assert len(a1_msgs) == 1
        assert a1_msgs[0]["msg_id"] == "m1"

        a2_msgs = await store.drain("a2")
        assert len(a2_msgs) == 1
        assert a2_msgs[0]["msg_id"] == "m2"


# ── PushEvent msg_id tests ───────────────────────────────────────────

class TestPushEventMsgId:
    def test_auto_generated_msg_id(self):
        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["a1"],
        )
        assert event.msg_id
        assert isinstance(event.msg_id, str)
        assert len(event.msg_id) == 32  # uuid4 hex

    def test_unique_msg_ids(self):
        e1 = PushEvent(type=PushEventType.TASK_BROADCAST, task_id="t1", recipients=["a1"])
        e2 = PushEvent(type=PushEventType.TASK_BROADCAST, task_id="t1", recipients=["a1"])
        assert e1.msg_id != e2.msg_id

    def test_custom_msg_id(self):
        event = PushEvent(
            msg_id="custom-id",
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["a1"],
        )
        assert event.msg_id == "custom-id"
