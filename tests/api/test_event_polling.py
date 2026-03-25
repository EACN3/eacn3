"""Event polling workflow tests.

Tests the HTTP-based event transport that replaced WebSocket:
- Events appear in queue when actions happen
- Polling drains events
- Long-polling works
- Multiple agents get their own events
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.offline_store import OfflineStore
from eacn3.network.api.routes import router as net_router, set_network, set_offline_store
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network


@pytest.fixture
async def env():
    """Wired environment: network + offline_store + HTTP client."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())

    offline_store = OfflineStore(db=db)

    # Wire push → queue (per-recipient msg_id to avoid UNIQUE conflict)
    import uuid as _uuid
    async def queue_push(event):
        for agent_id in event.recipients:
            await offline_store.store(
                msg_id=_uuid.uuid4().hex,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )
    net.push.set_handler(queue_push)

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(net)
    set_discovery_network(net)
    set_offline_store(offline_store)

    # Fund and register
    net.escrow.get_or_create_account("user1", 10_000.0)
    for aid in ("a1", "a2", "a3"):
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": offline_store}

    await db.close()


class TestEventPolling:
    @pytest.mark.asyncio
    async def test_bid_result_appears_in_queue(self, env):
        """When an agent bids, it gets a bid_result event in its queue."""
        c = env["client"]

        # Create task
        await c.post("/api/tasks", json={
            "task_id": "ep-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # Agent bids
        await c.post("/api/tasks/ep-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        # Poll for a1's events
        resp = await c.get("/api/events/a1", params={"timeout": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

        # Should contain bid_result event
        types = [e["type"] for e in data["events"]]
        assert "bid_result" in types

    @pytest.mark.asyncio
    async def test_task_broadcast_reaches_matching_agents(self, env):
        """Creating a task sends broadcast to matching domain agents."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # All coding agents should have task_broadcast
        for agent in ["a1", "a2", "a3"]:
            resp = await c.get(f"/api/events/{agent}", params={"timeout": 0})
            data = resp.json()
            types = [e["type"] for e in data["events"]]
            assert "task_broadcast" in types, f"{agent} missing broadcast"

    @pytest.mark.asyncio
    async def test_empty_poll_returns_empty(self, env):
        """Polling with no events returns empty list."""
        c = env["client"]
        resp = await c.get("/api/events/nobody", params={"timeout": 0})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_drain_clears_events(self, env):
        """After draining, events are gone from the queue."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-3", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # First drain
        resp1 = await c.get("/api/events/a1", params={"timeout": 0})
        assert resp1.json()["count"] > 0

        # Second drain should be empty
        resp2 = await c.get("/api/events/a1", params={"timeout": 0})
        assert resp2.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_each_agent_gets_own_events(self, env):
        """Agent a1's events don't leak to agent a2."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-4", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # a1 bids → gets bid_result
        await c.post("/api/tasks/ep-4/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        # Drain a1 → should have bid_result + broadcast
        resp_a1 = await c.get("/api/events/a1", params={"timeout": 0})
        a1_types = [e["type"] for e in resp_a1.json()["events"]]
        assert "bid_result" in a1_types

        # Drain a2 → should have broadcast but NOT bid_result
        resp_a2 = await c.get("/api/events/a2", params={"timeout": 0})
        a2_types = [e["type"] for e in resp_a2.json()["events"]]
        assert "bid_result" not in a2_types
        assert "task_broadcast" in a2_types

    @pytest.mark.asyncio
    async def test_task_collected_event(self, env):
        """When task reaches awaiting_retrieval, initiator gets task_collected."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-5", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/ep-5/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        await c.post("/api/tasks/ep-5/result", json={
            "agent_id": "a1", "content": "done",
        })

        # Drain first to clear broadcast/bid events
        await c.get("/api/events/user1", params={"timeout": 0})

        # Close task → triggers task_collected
        await c.post("/api/tasks/ep-5/close", json={"initiator_id": "user1"})

        resp = await c.get("/api/events/user1", params={"timeout": 0})
        types = [e["type"] for e in resp.json()["events"]]
        assert "task_collected" in types

    @pytest.mark.asyncio
    async def test_timeout_event_on_deadline_scan(self, env):
        """Deadline scan produces task_timeout event."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-6", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
            "deadline": "2020-01-01T00:00:00Z",
        })
        await c.post("/api/tasks/ep-6/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        # Clear existing events
        await c.get("/api/events/user1", params={"timeout": 0})
        await c.get("/api/events/a1", params={"timeout": 0})

        # Scan deadlines
        await c.post("/api/admin/scan-deadlines")

        # Both initiator and executor should get timeout
        for agent in ["user1", "a1"]:
            resp = await c.get(f"/api/events/{agent}", params={"timeout": 0})
            types = [e["type"] for e in resp.json()["events"]]
            assert "task_timeout" in types, f"{agent} missing timeout event"

    @pytest.mark.asyncio
    async def test_discussion_update_event(self, env):
        """Discussion update pushes to all bidders including pending (#79)."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ep-7", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/ep-7/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        # Clear events
        await c.get("/api/events/a1", params={"timeout": 0})

        # Initiator sends discussion update
        await c.post("/api/tasks/ep-7/discussions", json={
            "initiator_id": "user1", "message": "Please use async",
        })

        # a1 should get discussion_update
        resp = await c.get("/api/events/a1", params={"timeout": 0})
        types = [e["type"] for e in resp.json()["events"]]
        assert "discussion_update" in types
