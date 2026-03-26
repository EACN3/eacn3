"""Extreme concurrency stress tests.

Push the system to its limits with high-contention scenarios:
- 50 agents bid on one task simultaneously
- 20 concurrent operations on the same task
- Rapid create-close cycles
- Concurrent deposits while creating tasks
"""

import asyncio
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
    store = OfflineStore(db=db, max_per_agent=50, ttl_seconds=3600)
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

    # Register 50 agents
    await net.discovery.register_server(
        server_id="srv-ext", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(50):
        aid = f"ext-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:{9000 + i}", "server_id": "srv-ext",
        }
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 100_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net}
    await db.close()


class TestExtremeBidConcurrency:
    @pytest.mark.asyncio
    async def test_50_agents_bid_on_one_task(self, env):
        """50 agents all bid on the same task simultaneously."""
        c, net = env["client"], env["net"]

        await c.post("/api/tasks", json={
            "task_id": "ext-50bid", "initiator_id": "ext-0",
            "content": {}, "domains": ["coding"],
            "budget": 5000.0, "max_concurrent_bidders": 10,
        })

        results = await asyncio.gather(*[
            c.post("/api/tasks/ext-50bid/bid", json={
                "agent_id": f"ext-{i+1}", "confidence": 0.9, "price": 80.0,
            })
            for i in range(49)
        ])

        codes = [r.status_code for r in results]
        assert all(c in (200, 400) for c in codes), f"Server error: {codes}"

        ok_count = codes.count(200)
        assert ok_count == 49, f"Only {ok_count} bids succeeded"

        task = (await c.get("/api/tasks/ext-50bid")).json()
        executing = sum(1 for b in task["bids"] if b["status"] == "executing")
        waiting = sum(1 for b in task["bids"] if b["status"] == "waiting")
        assert executing == 10
        assert waiting == 39


class TestMixedConcurrentOps:
    @pytest.mark.asyncio
    async def test_bid_result_reject_close_concurrent(self, env):
        """Multiple different operations on the same task at once."""
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "ext-mixed", "initiator_id": "ext-0",
            "content": {}, "domains": ["coding"],
            "budget": 1000.0, "max_concurrent_bidders": 5,
        })

        # Pre-bid some agents
        for i in range(1, 4):
            await c.post("/api/tasks/ext-mixed/bid", json={
                "agent_id": f"ext-{i}", "confidence": 0.9, "price": 80.0,
            })

        # Now do everything at once
        results = await asyncio.gather(
            c.post("/api/tasks/ext-mixed/bid", json={
                "agent_id": "ext-5", "confidence": 0.9, "price": 80.0,
            }),
            c.post("/api/tasks/ext-mixed/result", json={
                "agent_id": "ext-1", "content": "done",
            }),
            c.post("/api/tasks/ext-mixed/reject", json={"agent_id": "ext-2"}),
            c.post("/api/tasks/ext-mixed/discussions", json={
                "initiator_id": "ext-0", "message": "hurry up",
            }),
        )

        # No 500 errors
        for r in results:
            assert r.status_code in (200, 400), f"Server error: {r.status_code} {r.text}"


class TestRapidCreateClose:
    @pytest.mark.asyncio
    async def test_30_rapid_create_close_cycles(self, env):
        """Create and immediately close 30 tasks in parallel."""
        c = env["client"]

        async def create_and_close(i):
            tid = f"ext-cc-{i}"
            r1 = await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "ext-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            })
            if r1.status_code != 201:
                return "create_failed"
            r2 = await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": "ext-0",
            })
            return "ok" if r2.status_code == 200 else "close_failed"

        results = await asyncio.gather(*[create_and_close(i) for i in range(30)])
        ok_count = results.count("ok")
        assert ok_count == 30, f"Only {ok_count}/30 succeeded: {results}"


class TestConcurrentDepositsAndTasks:
    @pytest.mark.asyncio
    async def test_deposits_while_creating_tasks(self, env):
        """10 deposits + 10 task creates for same agent, all concurrent."""
        c = env["client"]

        coros = []
        for i in range(10):
            coros.append(c.post("/api/economy/deposit", json={
                "agent_id": "ext-0", "amount": 100.0,
            }))
            coros.append(c.post("/api/tasks", json={
                "task_id": f"ext-dep-{i}", "initiator_id": "ext-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            }))

        results = await asyncio.gather(*coros)
        for r in results:
            assert r.status_code in (200, 201, 402), f"Unexpected: {r.status_code}"


class TestHighContention:
    @pytest.mark.asyncio
    async def test_10_agents_all_operations_concurrent(self, env):
        """10 agents each doing create+bid+result in parallel."""
        c = env["client"]

        async def agent_flow(i):
            tid = f"ext-hc-{i}"
            initiator = f"ext-{i}"
            executor = f"ext-{(i + 1) % 50}"

            r = await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": initiator,
                "content": {}, "domains": ["coding"], "budget": 200.0,
            })
            if r.status_code != 201:
                return "create_failed"

            r = await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": executor, "confidence": 0.9, "price": 80.0,
            })
            if r.status_code != 200:
                return "bid_failed"

            r = await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": executor, "content": "done",
            })
            if r.status_code != 200:
                return "result_failed"

            r = await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": initiator,
            })
            if r.status_code != 200:
                return "close_failed"

            r = await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": initiator, "agent_id": executor,
            })
            if r.status_code != 200:
                return "select_failed"

            return "completed"

        results = await asyncio.gather(*[agent_flow(i) for i in range(10)])
        completed = results.count("completed")
        assert completed == 10, f"Only {completed}/10 completed: {results}"
