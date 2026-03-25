"""Tests: Offline message cache and ACK-based reliable delivery.

Covers:
- OfflineStore: store, drain, count, prune
- ConnectionManager: ACK tracking, offline caching on delivery failure
- PushEvent: msg_id generation
"""

import asyncio

import pytest

from eacn3.core.models import PushEvent, PushEventType
from eacn3.network.api.websocket import ConnectionManager
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


# ── ConnectionManager ACK tests ─────────────────────────────────────

class TestConnectionManagerAck:
    @pytest.fixture
    def mgr(self):
        m = ConnectionManager()
        m.ack_timeout = 1  # Short timeout for tests
        return m

    def test_handle_ack_no_pending(self, mgr):
        assert mgr.handle_ack("nonexistent") is False

    @pytest.mark.asyncio
    async def test_handle_ack_resolves_pending(self, mgr):
        """ACK should set the pending event."""
        event = asyncio.Event()
        mgr._pending_acks["m1"] = event
        assert mgr.handle_ack("m1") is True
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_broadcast_to_disconnected_stores_offline(self, mgr):
        """When no agent is connected, messages should go to offline store."""
        db = Database()
        await db.connect()
        store = OfflineStore(db, max_per_agent=100, ttl_seconds=3600)
        mgr.set_offline_store(store)

        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["offline-agent"],
            payload={"data": "test"},
        )
        delivered = await mgr.broadcast_event(event)
        assert delivered == 0

        # Verify message was stored offline
        count = await store.count("offline-agent")
        assert count == 1

        messages = await store.drain("offline-agent")
        assert messages[0]["msg_id"] == event.msg_id
        assert messages[0]["payload"] == {"data": "test"}

        await db.close()

    @pytest.mark.asyncio
    async def test_broadcast_no_offline_store_no_crash(self, mgr):
        """Without offline store, undelivered messages are dropped silently."""
        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["a1"],
            payload={},
        )
        delivered = await mgr.broadcast_event(event)
        assert delivered == 0
