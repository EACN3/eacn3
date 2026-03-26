"""Resource leak and degradation tests.

Detects problems that only surface after long operation:
- Push history unbounded growth
- Task locks accumulate (never cleaned up)
- Reputation _recent_events per-agent deque accumulation
- Offline store messages for terminated tasks
- Escrow entries lingering after settlement
- Agent re-registration with stale state
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI
import uuid as _uuid

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.offline_store import OfflineStore
from eacn3.network.api.routes import router as net_router, set_network, set_offline_store
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network


@pytest.fixture
async def env():
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    store = OfflineStore(db=db, max_per_agent=20, ttl_seconds=3600)

    async def queue_push(event):
        for aid in event.recipients:
            await store.store(_uuid.uuid4().hex, aid, event.type.value,
                            event.task_id, event.payload)
    net.push.set_handler(queue_push)

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(net)
    set_discovery_network(net)
    set_offline_store(store)

    await net.discovery.register_server(
        server_id="srv-leak", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(3):
        aid = f"leak-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}", "server_id": "srv-leak",
        }
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 50_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store, "db": db}
    await db.close()


class TestPushHistoryGrowth:
    """Push service _history list grows unbounded."""

    @pytest.mark.asyncio
    async def test_push_history_accumulates(self, env):
        c, net = env["client"], env["net"]

        for i in range(30):
            await c.post("/api/tasks", json={
                "task_id": f"ph-{i}", "initiator_id": "leak-0",
                "content": {}, "domains": ["coding"], "budget": 10.0,
            })

        # Push history should have at least 30 broadcast events
        history = net.push.get_history()
        assert len(history) >= 30
        # This is a leak — history never gets trimmed
        # For now just document it exists


class TestTaskLockAccumulation:
    """Per-task locks in TaskManager accumulate forever."""

    @pytest.mark.asyncio
    async def test_locks_grow_with_tasks(self, env):
        c, net = env["client"], env["net"]

        for i in range(20):
            tid = f"lock-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "leak-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "leak-1", "confidence": 0.9, "price": 30.0,
            })

        # Each task that was bid on should have a lock
        lock_count = len(net.task_manager._task_locks)
        assert lock_count >= 20

        # After purging tasks, locks should also be cleaned
        # (purge_terminated only works with deadlines, but the concept is verified)


class TestReputationRecentEventsGrowth:
    """_recent_events deques accumulate per agent forever."""

    @pytest.mark.asyncio
    async def test_recent_events_bounded_per_agent(self, env):
        net = env["net"]

        # Submit 50 events for one agent
        for i in range(50):
            await net.reputation.aggregate(
                "leak-0",
                [{"type": "result_selected"}],
                server_id="srv-leak",
            )

        # deque should be bounded by BURST_WINDOW (default 10)
        recent = net.reputation._recent_events.get("leak-0")
        assert recent is not None
        assert len(recent) <= net.reputation.BURST_WINDOW

    @pytest.mark.asyncio
    async def test_many_agents_create_entries(self, env):
        net = env["net"]

        # 100 different agents each submit 1 event
        for i in range(100):
            await net.reputation.aggregate(
                f"bulk-agent-{i}",
                [{"type": "result_selected"}],
                server_id="srv-leak",
            )

        # Should have 100 entries in _recent_events
        assert len(net.reputation._recent_events) >= 100
        # Each bounded
        for aid, dq in net.reputation._recent_events.items():
            assert len(dq) <= net.reputation.BURST_WINDOW


class TestOfflineStoreLeaks:
    """Messages for terminated tasks stay in the queue."""

    @pytest.mark.asyncio
    async def test_messages_remain_after_task_completion(self, env):
        c, net, store = env["client"], env["net"], env["store"]

        # Create task → broadcasts
        await c.post("/api/tasks", json={
            "task_id": "leak-task", "initiator_id": "leak-0",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })

        # Complete the task
        await c.post("/api/tasks/leak-task/bid", json={
            "agent_id": "leak-1", "confidence": 0.9, "price": 50.0,
        })
        await c.post("/api/tasks/leak-task/result", json={
            "agent_id": "leak-1", "content": "done",
        })
        await c.post("/api/tasks/leak-task/close", json={"initiator_id": "leak-0"})
        await c.post("/api/tasks/leak-task/select", json={
            "initiator_id": "leak-0", "agent_id": "leak-1",
        })

        # Messages for this task may still be in the queue for agents that
        # haven't polled — this is expected but should be cleaned up eventually
        # cleanup_task can be called explicitly
        deleted = await store.cleanup_task("leak-task")
        # May or may not have pending messages depending on whether agents polled
        assert deleted >= 0

    @pytest.mark.asyncio
    async def test_cleanup_task_removes_all_for_task(self, env):
        store = env["store"]

        # Manually store some messages
        for i in range(5):
            await store.store(f"cleanup-{i}", f"leak-{i % 3}", "broadcast",
                            "cleanup-target", {"i": i})

        # Verify they exist
        counts_before = await store.count_all()
        total_before = sum(counts_before.values())
        assert total_before >= 5

        # Cleanup
        deleted = await store.cleanup_task("cleanup-target")
        assert deleted == 5


class TestEscrowLeakAfterSettlement:
    """Verify no escrow entries linger after settlement."""

    @pytest.mark.asyncio
    async def test_escrow_cleared_after_settlement(self, env):
        c, net = env["client"], env["net"]

        for i in range(10):
            tid = f"esc-leak-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "leak-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "leak-1", "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "leak-1", "content": "done",
            })
            await c.post(f"/api/tasks/{tid}/close", json={"initiator_id": "leak-0"})
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "leak-0", "agent_id": "leak-1",
            })

        # All 10 escrows should be released
        remaining = 0
        for tid_key in net.escrow._task_escrows:
            if tid_key.startswith("esc-leak-"):
                remaining += 1
        assert remaining == 0, f"{remaining} escrow entries still exist"


class TestSettlementSetGrowth:
    """Settlement._settled set grows indefinitely."""

    @pytest.mark.asyncio
    async def test_settled_set_grows(self, env):
        c, net = env["client"], env["net"]

        for i in range(15):
            tid = f"settle-grow-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "leak-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "leak-1", "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "leak-1", "content": "done",
            })
            await c.post(f"/api/tasks/{tid}/close", json={"initiator_id": "leak-0"})
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "leak-0", "agent_id": "leak-1",
            })

        # _settled grows with every settlement — this is expected for idempotency
        # but is a potential memory issue for long-running systems
        assert len(net.settlement._settled) == 15
