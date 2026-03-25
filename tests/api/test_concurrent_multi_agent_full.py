"""Full multi-agent concurrent simulation.

Simulates a realistic multi-agent session: N agents connected to the same
server, each performing independent tasks while also competing on shared tasks.
This is the closest test to the real-world usage that was failing.
"""

import asyncio
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
async def sim():
    """Full simulation environment with N agents pre-registered."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    store = OfflineStore(db=db)

    async def queue_push(event):
        for aid in event.recipients:
            await store.store(uuid.uuid4().hex, aid, event.type.value,
                            event.task_id, event.payload)
    net.push.set_handler(queue_push)

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(net)
    set_discovery_network(net)
    set_offline_store(store)

    # Register server
    await net.discovery.register_server(
        server_id="srv-sim", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )

    # Register 10 agents on 2 domains
    agents = {}
    for i in range(10):
        aid = f"sim-{i}"
        domains = ["backend"] if i < 5 else ["frontend"]
        card = {
            "agent_id": aid, "name": f"Agent {i}",
            "domains": domains,
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}",
            "server_id": "srv-sim",
        }
        await net.discovery.register_agent(card)
        for d in domains:
            await net.dht.announce(d, aid)
        net.reputation._scores[aid] = 0.7 + i * 0.02
        net.escrow.get_or_create_account(aid, 5000.0)
        agents[aid] = {"domains": domains}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store, "agents": agents}
    await db.close()


class TestMultiAgentSimulation:
    """Simulates realistic multi-agent concurrent usage."""

    @pytest.mark.asyncio
    async def test_5_initiators_create_tasks_concurrently(self, sim):
        """5 agents each create a task targeting the other domain."""
        c = sim["client"]

        # Backend agents create frontend tasks and vice versa
        tasks = await asyncio.gather(*[
            c.post("/api/tasks", json={
                "task_id": f"sim-task-{i}",
                "initiator_id": f"sim-{i}",
                "content": {"desc": f"task from sim-{i}"},
                "domains": ["frontend"] if i < 5 else ["backend"],
                "budget": 200.0,
                "max_concurrent_bidders": 3,
            })
            for i in range(5)
        ])
        assert all(r.status_code == 201 for r in tasks)

    @pytest.mark.asyncio
    async def test_agents_receive_broadcasts_for_their_domain(self, sim):
        """Frontend agents get frontend broadcasts, backend get backend."""
        c = sim["client"]

        # Create a backend task
        await c.post("/api/tasks", json={
            "task_id": "domain-test",
            "initiator_id": "sim-5",  # frontend agent creating backend task
            "content": {}, "domains": ["backend"], "budget": 200.0,
        })

        # Backend agents (sim-0 to sim-4) should have broadcast
        for i in range(5):
            resp = await c.get(f"/api/events/sim-{i}", params={"timeout": 0})
            types = [e["type"] for e in resp.json()["events"]]
            assert "task_broadcast" in types, f"sim-{i} (backend) missing broadcast"

        # Frontend agents (sim-5 to sim-9) should NOT have broadcast
        for i in range(5, 10):
            resp = await c.get(f"/api/events/sim-{i}", params={"timeout": 0})
            types = [e["type"] for e in resp.json()["events"]]
            assert "task_broadcast" not in types, f"sim-{i} (frontend) got wrong broadcast"

    @pytest.mark.asyncio
    async def test_full_lifecycle_concurrent(self, sim):
        """3 tasks, each with different agents bidding, submitting, selecting."""
        c = sim["client"]

        # Phase 1: Create 3 tasks
        for i in range(3):
            await c.post("/api/tasks", json={
                "task_id": f"lc-{i}",
                "initiator_id": f"sim-{i}",  # sim-0,1,2 create
                "content": {"desc": f"lifecycle {i}"},
                "domains": ["backend"],
                "budget": 300.0,
                "max_concurrent_bidders": 2,
            })

        # Clear broadcasts
        for i in range(10):
            await c.get(f"/api/events/sim-{i}", params={"timeout": 0})

        # Phase 2: Backend agents bid on all tasks concurrently
        bid_coros = []
        for task_i in range(3):
            for agent_i in range(3, 5):  # sim-3, sim-4 bid
                bid_coros.append(c.post(f"/api/tasks/lc-{task_i}/bid", json={
                    "agent_id": f"sim-{agent_i}",
                    "confidence": 0.9,
                    "price": 80.0 + agent_i,
                }))
        bid_results = await asyncio.gather(*bid_coros)
        assert all(r.status_code == 200 for r in bid_results)

        # Phase 3: Both agents submit results for all tasks
        for task_i in range(3):
            for agent_i in range(3, 5):
                resp = await c.post(f"/api/tasks/lc-{task_i}/result", json={
                    "agent_id": f"sim-{agent_i}",
                    "content": f"result from sim-{agent_i}",
                })
                # May fail if auto-collect already fired
                assert resp.status_code in (200, 400)

        # Phase 4: Initiators close and select
        for i in range(3):
            await c.post(f"/api/tasks/lc-{i}/close", json={
                "initiator_id": f"sim-{i}",
            })
            # Select sim-3's result
            resp = await c.post(f"/api/tasks/lc-{i}/select", json={
                "initiator_id": f"sim-{i}",
                "agent_id": "sim-3",
            })
            assert resp.status_code == 200

        # Phase 5: Verify all tasks completed
        for i in range(3):
            task = (await c.get(f"/api/tasks/lc-{i}")).json()
            assert task["status"] == "completed"

        # sim-3 should have been paid 3 times
        bal = (await c.get("/api/economy/balance",
                          params={"agent_id": "sim-3"})).json()
        assert bal["available"] > 5000.0  # Started with 5000 + 3 payments

    @pytest.mark.asyncio
    async def test_10_agents_polling_events_concurrently(self, sim):
        """All 10 agents poll for events simultaneously after task creation."""
        c = sim["client"]

        await c.post("/api/tasks", json={
            "task_id": "poll-sim",
            "initiator_id": "sim-0",
            "content": {}, "domains": ["backend"], "budget": 100.0,
        })

        # All 10 agents poll concurrently
        results = await asyncio.gather(*[
            c.get(f"/api/events/sim-{i}", params={"timeout": 0})
            for i in range(10)
        ])
        assert all(r.status_code == 200 for r in results)

    @pytest.mark.asyncio
    async def test_agent_creates_task_and_subtask_concurrently(self, sim):
        """One agent creates a task, bids, creates subtask — all while others bid."""
        c = sim["client"]

        # sim-0 creates a task
        await c.post("/api/tasks", json={
            "task_id": "sub-sim",
            "initiator_id": "sim-0",
            "content": {}, "domains": ["backend"],
            "budget": 500.0,
            "max_concurrent_bidders": 3,
        })

        # sim-3 and sim-4 bid concurrently
        await asyncio.gather(
            c.post("/api/tasks/sub-sim/bid", json={
                "agent_id": "sim-3", "confidence": 0.9, "price": 200.0,
            }),
            c.post("/api/tasks/sub-sim/bid", json={
                "agent_id": "sim-4", "confidence": 0.9, "price": 180.0,
            }),
        )

        # sim-3 creates a subtask while sim-4 submits result
        sub_resp, result_resp = await asyncio.gather(
            c.post("/api/tasks/sub-sim/subtask", json={
                "initiator_id": "sim-3", "content": {},
                "domains": ["frontend"], "budget": 50.0,
            }),
            c.post("/api/tasks/sub-sim/result", json={
                "agent_id": "sim-4", "content": "done by sim-4",
            }),
        )
        assert sub_resp.status_code == 201
        assert result_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rapid_deposit_bid_result_select(self, sim):
        """Rapid cycle: deposit → create → bid → result → select, 5 times."""
        c = sim["client"]

        async def cycle(i: int):
            initiator = f"sim-{i % 3}"
            executor = f"sim-{3 + i % 2}"
            tid = f"rapid-sim-{i}"

            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": initiator,
                "content": {}, "domains": ["backend"],
                "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": executor, "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": executor, "content": f"result-{i}",
            })
            await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": initiator,
            })
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": initiator, "agent_id": executor,
            })
            return (await c.get(f"/api/tasks/{tid}")).json()["status"]

        statuses = await asyncio.gather(*[cycle(i) for i in range(5)])
        assert all(s == "completed" for s in statuses), f"Not all completed: {statuses}"
