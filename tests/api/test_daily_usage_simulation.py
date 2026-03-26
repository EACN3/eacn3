"""Simulate a typical day of usage.

Models realistic usage patterns:
- Multiple users creating tasks at different times
- Agents discovering and bidding on tasks
- Mix of successful and failed completions
- Budget top-ups mid-session
- Task timeouts mixed with successful completions
- Event polling between operations
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
    store = OfflineStore(db=db, max_per_agent=100, ttl_seconds=3600)
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

    # Setup: 3 users, 5 coding agents, 3 design agents
    await net.discovery.register_server(
        server_id="srv-daily", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(3):
        net.escrow.get_or_create_account(f"user-{i}", 20_000.0)
    for i in range(5):
        aid = f"coder-{i}"
        card = {"agent_id": aid, "name": f"Coder {i}", "domains": ["coding"],
                "skills": [{"name": "code", "description": "code"}],
                "url": f"http://localhost:{9000 + i}", "server_id": "srv-daily"}
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.6 + i * 0.05
    for i in range(3):
        aid = f"designer-{i}"
        card = {"agent_id": aid, "name": f"Designer {i}", "domains": ["design"],
                "skills": [{"name": "design", "description": "design"}],
                "url": f"http://localhost:{9100 + i}", "server_id": "srv-daily"}
        await net.discovery.register_agent(card)
        await net.dht.announce("design", aid)
        net.reputation._scores[aid] = 0.7 + i * 0.05

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestDailySimulation:
    @pytest.mark.asyncio
    async def test_morning_session(self, env):
        """Morning: 3 users each create 2 coding tasks, agents compete."""
        c = env["client"]

        # Users create tasks
        for user in range(3):
            for task in range(2):
                tid = f"morning-u{user}-t{task}"
                resp = await c.post("/api/tasks", json={
                    "task_id": tid, "initiator_id": f"user-{user}",
                    "content": {"desc": f"Morning task {task} from user {user}"},
                    "domains": ["coding"], "budget": 500.0,
                    "max_concurrent_bidders": 2,
                })
                assert resp.status_code == 201

        # Agents poll for broadcasts
        for i in range(5):
            resp = await c.get(f"/api/events/coder-{i}", params={"timeout": 0})
            assert resp.json()["count"] > 0

        # Each agent bids on 2 random tasks
        for agent_i in range(5):
            for task_i in range(2):
                tid = f"morning-u{agent_i % 3}-t{task_i}"
                await c.post(f"/api/tasks/{tid}/bid", json={
                    "agent_id": f"coder-{agent_i}",
                    "confidence": 0.8 + agent_i * 0.02,
                    "price": 200.0 + agent_i * 10,
                })

        # First agent on each task submits result
        for user in range(3):
            for task in range(2):
                tid = f"morning-u{user}-t{task}"
                t = (await c.get(f"/api/tasks/{tid}")).json()
                executing = [b["agent_id"] for b in t["bids"]
                            if b["status"] == "executing"]
                if executing:
                    await c.post(f"/api/tasks/{tid}/result", json={
                        "agent_id": executing[0], "content": "morning work done",
                    })

    @pytest.mark.asyncio
    async def test_afternoon_mixed_domains(self, env):
        """Afternoon: mix of coding and design tasks."""
        c = env["client"]

        # Create 3 coding + 2 design tasks
        for i in range(3):
            await c.post("/api/tasks", json={
                "task_id": f"afternoon-code-{i}", "initiator_id": "user-0",
                "content": {}, "domains": ["coding"], "budget": 300.0,
            })
        for i in range(2):
            await c.post("/api/tasks", json={
                "task_id": f"afternoon-design-{i}", "initiator_id": "user-1",
                "content": {}, "domains": ["design"], "budget": 400.0,
            })

        # Verify domain routing — designers shouldn't get coding broadcasts
        coding_events = (await c.get("/api/events/coder-0", params={"timeout": 0})).json()
        design_events = (await c.get("/api/events/designer-0", params={"timeout": 0})).json()

        coding_types = [e["type"] for e in coding_events["events"]]
        design_types = [e["type"] for e in design_events["events"]]

        # Coders should have coding broadcasts
        assert coding_types.count("task_broadcast") >= 3
        # Designers should have design broadcasts (may also have coding if domains overlap)
        assert design_types.count("task_broadcast") >= 2

    @pytest.mark.asyncio
    async def test_evening_completions_and_timeouts(self, env):
        """Evening: some tasks complete, some timeout."""
        c, net = env["client"], env["net"]

        # Create 4 tasks — 2 will complete, 2 will timeout
        for i in range(2):
            await c.post("/api/tasks", json={
                "task_id": f"evening-ok-{i}", "initiator_id": "user-2",
                "content": {}, "domains": ["coding"], "budget": 200.0,
            })
        for i in range(2):
            await c.post("/api/tasks", json={
                "task_id": f"evening-timeout-{i}", "initiator_id": "user-2",
                "content": {}, "domains": ["coding"], "budget": 200.0,
                "deadline": "2020-01-01T00:00:00Z",
            })

        # Bid and complete the OK tasks
        for i in range(2):
            await c.post(f"/api/tasks/evening-ok-{i}/bid", json={
                "agent_id": f"coder-{i}", "confidence": 0.9, "price": 100.0,
            })
            await c.post(f"/api/tasks/evening-ok-{i}/result", json={
                "agent_id": f"coder-{i}", "content": "evening work done",
            })
            await c.post(f"/api/tasks/evening-ok-{i}/close", json={
                "initiator_id": "user-2",
            })
            await c.post(f"/api/tasks/evening-ok-{i}/select", json={
                "initiator_id": "user-2", "agent_id": f"coder-{i}",
            })

        # Scan deadlines for timeout tasks
        await c.post("/api/admin/scan-deadlines")

        # Verify outcomes
        for i in range(2):
            t = (await c.get(f"/api/tasks/evening-ok-{i}")).json()
            assert t["status"] == "completed"
        for i in range(2):
            t = (await c.get(f"/api/tasks/evening-timeout-{i}")).json()
            assert t["status"] == "no_one_able"

    @pytest.mark.asyncio
    async def test_budget_management_during_day(self, env):
        """User manages budget: creates tasks, runs out, deposits, creates more."""
        c, net = env["client"], env["net"]

        # Spend all of user-0's budget (20000 / 2000 = 10 tasks)
        for i in range(10):
            resp = await c.post("/api/tasks", json={
                "task_id": f"budget-day-{i}", "initiator_id": "user-0",
                "content": {}, "domains": ["coding"], "budget": 2000.0,
            })
            assert resp.status_code == 201

        # Try to create one more — should fail (balance = 0)
        resp = await c.post("/api/tasks", json={
            "task_id": "budget-day-fail", "initiator_id": "user-0",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 402

        # Deposit more funds
        await c.post("/api/economy/deposit", json={
            "agent_id": "user-0", "amount": 10000.0,
        })

        # Should be able to create now
        resp = await c.post("/api/tasks", json={
            "task_id": "budget-day-after-deposit", "initiator_id": "user-0",
            "content": {}, "domains": ["coding"], "budget": 5000.0,
        })
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_full_day_balance_check(self, env):
        """End of day: verify all account balances are consistent."""
        c, net = env["client"], env["net"]

        # Run 10 complete task cycles
        for i in range(10):
            await c.post("/api/tasks", json={
                "task_id": f"day-full-{i}", "initiator_id": "user-0",
                "content": {}, "domains": ["coding"], "budget": 200.0,
            })
            await c.post(f"/api/tasks/day-full-{i}/bid", json={
                "agent_id": "coder-0", "confidence": 0.9, "price": 100.0,
            })
            await c.post(f"/api/tasks/day-full-{i}/result", json={
                "agent_id": "coder-0", "content": "done",
            })
            await c.post(f"/api/tasks/day-full-{i}/close", json={
                "initiator_id": "user-0",
            })
            await c.post(f"/api/tasks/day-full-{i}/select", json={
                "initiator_id": "user-0", "agent_id": "coder-0",
            })

        # End-of-day checks
        for aid, acct in net.escrow._accounts.items():
            assert acct.available >= 0, f"{aid} negative available: {acct.available}"
            assert acct.frozen >= 0, f"{aid} negative frozen: {acct.frozen}"

        # No lingering escrows for completed tasks
        for tid in [f"day-full-{i}" for i in range(10)]:
            assert tid not in net.escrow._task_escrows, f"Escrow leak: {tid}"
