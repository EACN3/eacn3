"""End-to-end push event flow tests.

Tests the complete chain: action → push service → queue handler → offline store → poll.
Verifies that every important user action produces the expected events to the right recipients.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI
import uuid

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
    store = OfflineStore(db=db)

    async def queue_push(event):
        for agent_id in event.recipients:
            await store.store(uuid.uuid4().hex, agent_id, event.type.value,
                            event.task_id, event.payload)
    net.push.set_handler(queue_push)

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(net)
    set_discovery_network(net)
    set_offline_store(store)

    net.escrow.get_or_create_account("user1", 10_000.0)
    for aid in ("a1", "a2", "a3"):
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


async def _drain(c, aid):
    return (await c.get(f"/api/events/{aid}", params={"timeout": 0})).json()


async def _clear_events(c, *agents):
    for a in agents:
        await c.get(f"/api/events/{a}", params={"timeout": 0})


class TestBroadcastEventFlow:
    @pytest.mark.asyncio
    async def test_task_create_broadcasts_to_matching_agents(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "pef-1", "initiator_id": "user1",
            "content": {"desc": "work"}, "domains": ["coding"], "budget": 200.0,
        })

        for aid in ("a1", "a2", "a3"):
            data = await _drain(c, aid)
            types = [e["type"] for e in data["events"]]
            assert "task_broadcast" in types, f"{aid} missing task_broadcast"

    @pytest.mark.asyncio
    async def test_broadcast_contains_task_details(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "pef-2", "initiator_id": "user1",
            "content": {"desc": "detailed"}, "domains": ["coding"],
            "budget": 300.0, "deadline": "2025-12-31T00:00:00Z",
        })

        data = await _drain(c, "a1")
        bc = next(e for e in data["events"] if e["type"] == "task_broadcast")
        assert bc["task_id"] == "pef-2"
        assert bc["payload"]["budget"] == 300.0


class TestBidEventFlow:
    @pytest.mark.asyncio
    async def test_bid_produces_bid_result_event(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "bef-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await _clear_events(c, "a1")

        await c.post("/api/tasks/bef-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        data = await _drain(c, "a1")
        types = [e["type"] for e in data["events"]]
        assert "bid_result" in types

        br = next(e for e in data["events"] if e["type"] == "bid_result")
        assert br["payload"]["accepted"] is True

    @pytest.mark.asyncio
    async def test_rejected_bid_event(self, env):
        c, net = env["client"], env["net"]
        net.reputation._scores["low-rep"] = 0.1
        await net.dht.announce("coding", "low-rep")

        await c.post("/api/tasks", json={
            "task_id": "bef-2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await _clear_events(c, "low-rep")

        await c.post("/api/tasks/bef-2/bid", json={
            "agent_id": "low-rep", "confidence": 0.3, "price": 80.0,
        })

        data = await _drain(c, "low-rep")
        types = [e["type"] for e in data["events"]]
        assert "bid_result" in types
        br = next(e for e in data["events"] if e["type"] == "bid_result")
        assert br["payload"]["accepted"] is False


class TestDiscussionEventFlow:
    @pytest.mark.asyncio
    async def test_discussion_reaches_all_bidders(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "def-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 500.0,
            "max_concurrent_bidders": 3,
        })
        for aid in ("a1", "a2"):
            await c.post("/api/tasks/def-1/bid", json={
                "agent_id": aid, "confidence": 0.9, "price": 80.0,
            })
        await _clear_events(c, "a1", "a2")

        await c.post("/api/tasks/def-1/discussions", json={
            "initiator_id": "user1", "message": "Please add tests",
        })

        for aid in ("a1", "a2"):
            data = await _drain(c, aid)
            types = [e["type"] for e in data["events"]]
            assert "discussion_update" in types, f"{aid} missing discussion_update"


class TestTimeoutEventFlow:
    @pytest.mark.asyncio
    async def test_timeout_event_to_initiator_and_executor(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "tef-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
            "deadline": "2020-01-01T00:00:00Z",
        })
        await c.post("/api/tasks/tef-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        await _clear_events(c, "user1", "a1")

        await c.post("/api/admin/scan-deadlines")

        for aid in ("user1", "a1"):
            data = await _drain(c, aid)
            types = [e["type"] for e in data["events"]]
            assert "task_timeout" in types, f"{aid} missing task_timeout"


class TestCollectedEventFlow:
    @pytest.mark.asyncio
    async def test_close_produces_collected_event(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "cef-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/cef-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        await c.post("/api/tasks/cef-1/result", json={
            "agent_id": "a1", "content": "done",
        })
        await _clear_events(c, "user1")

        await c.post("/api/tasks/cef-1/close", json={"initiator_id": "user1"})

        data = await _drain(c, "user1")
        types = [e["type"] for e in data["events"]]
        assert "task_collected" in types


class TestBudgetConfirmationEventFlow:
    @pytest.mark.asyncio
    async def test_over_budget_bid_sends_confirmation_request(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "bcef-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        await _clear_events(c, "user1")

        await c.post("/api/tasks/bcef-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        data = await _drain(c, "user1")
        types = [e["type"] for e in data["events"]]
        assert "bid_request_confirmation" in types

        conf = next(e for e in data["events"] if e["type"] == "bid_request_confirmation")
        assert conf["payload"]["agent_id"] == "a1"
        assert conf["payload"]["price"] == 80.0
