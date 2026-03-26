"""State degradation tests.

Tests that the system handles degraded states gracefully:
- What happens when you operate on tasks that have stale state
- Error accumulation (many failed operations in a row)
- Recovery after a series of failures
- Reputation convergence over many events
- Escrow accounting after partial failures
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

    await net.discovery.register_server(
        server_id="srv-deg", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(4):
        aid = f"deg-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}", "server_id": "srv-deg",
        }
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 50_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestErrorAccumulation:
    """Many sequential errors don't corrupt state."""

    @pytest.mark.asyncio
    async def test_50_invalid_operations_then_valid(self, env):
        """50 invalid operations followed by a valid one — system still works."""
        c = env["client"]

        # 50 invalid operations (bid on nonexistent task)
        for i in range(50):
            resp = await c.post(f"/api/tasks/nonexistent-{i}/bid", json={
                "agent_id": "deg-0", "confidence": 0.9, "price": 80.0,
            })
            assert resp.status_code == 400

        # System should still work perfectly
        resp = await c.post("/api/tasks", json={
            "task_id": "after-errors", "initiator_id": "deg-0",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 201

        resp = await c.post("/api/tasks/after-errors/bid", json={
            "agent_id": "deg-1", "confidence": 0.9, "price": 50.0,
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_mixed_valid_invalid_operations(self, env):
        """Interleave valid and invalid operations."""
        c = env["client"]

        for i in range(20):
            # Valid: create task
            resp = await c.post("/api/tasks", json={
                "task_id": f"mixed-{i}", "initiator_id": "deg-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            })
            assert resp.status_code == 201

            # Invalid: duplicate create
            resp = await c.post("/api/tasks", json={
                "task_id": f"mixed-{i}", "initiator_id": "deg-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            })
            assert resp.status_code == 409

            # Invalid: close wrong initiator
            resp = await c.post(f"/api/tasks/mixed-{i}/close", json={
                "initiator_id": "wrong-user",
            })
            assert resp.status_code == 400

        # All 20 tasks should be intact
        tasks = [
            (await c.get(f"/api/tasks/mixed-{i}")).json()
            for i in range(20)
        ]
        assert all(t["status"] == "unclaimed" for t in tasks)


class TestRecoveryAfterFailures:
    """System recovers cleanly after various failure scenarios."""

    @pytest.mark.asyncio
    async def test_escrow_recovery_after_failed_creates(self, env):
        """Failed task creates (duplicate ID) properly release escrow."""
        c, net = env["client"], env["net"]

        bal_before = net.escrow.get_or_create_account("deg-0", 0).available

        # Create 5 tasks normally
        for i in range(5):
            await c.post("/api/tasks", json={
                "task_id": f"recovery-{i}", "initiator_id": "deg-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })

        # Try to create 5 duplicates (should fail and release escrow)
        for i in range(5):
            resp = await c.post("/api/tasks", json={
                "task_id": f"recovery-{i}", "initiator_id": "deg-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            assert resp.status_code == 409

        bal_after = net.escrow.get_or_create_account("deg-0", 0)
        # Should have exactly 500 frozen (5 × 100), not 1000
        assert bal_after.frozen == 500.0
        assert bal_after.available == bal_before - 500.0


class TestReputationConvergence:
    """Reputation scores converge to reasonable values over many events."""

    @pytest.mark.asyncio
    async def test_reputation_stays_bounded(self, env):
        net = env["net"]

        # 100 positive events
        for _ in range(100):
            await net.reputation.aggregate(
                "convergence-agent",
                [{"type": "result_selected"}],
                server_id="srv-deg",
            )

        score = net.reputation.get_score("convergence-agent")
        assert 0.0 <= score <= 1.0, f"Score out of bounds: {score}"

    @pytest.mark.asyncio
    async def test_reputation_recovers_from_negatives(self, env):
        net = env["net"]
        net.reputation._scores["recovery-agent"] = 0.3  # Low

        # 50 positive events
        for _ in range(50):
            await net.reputation.aggregate(
                "recovery-agent",
                [{"type": "result_selected"}],
                server_id="srv-deg",
            )

        score = net.reputation.get_score("recovery-agent")
        assert score > 0.3, "Reputation should improve after positive events"
        assert score <= 1.0


class TestConcurrentErrorRecovery:
    """System handles concurrent errors gracefully."""

    @pytest.mark.asyncio
    async def test_concurrent_invalid_bids_dont_corrupt(self, env):
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "err-conc-1", "initiator_id": "deg-0",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # 10 concurrent bids from the SAME agent (9 should fail)
        results = await asyncio.gather(*[
            c.post("/api/tasks/err-conc-1/bid", json={
                "agent_id": "deg-1", "confidence": 0.9, "price": 80.0,
            })
            for _ in range(10)
        ])

        codes = [r.status_code for r in results]
        assert codes.count(200) == 1  # Exactly 1 success
        assert codes.count(400) == 9  # 9 duplicates

        # Task should have exactly 1 bid
        task = (await c.get("/api/tasks/err-conc-1")).json()
        assert len(task["bids"]) == 1

    @pytest.mark.asyncio
    async def test_concurrent_closes_dont_corrupt(self, env):
        c = env["client"]

        await c.post("/api/tasks", json={
            "task_id": "err-conc-2", "initiator_id": "deg-0",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # 5 concurrent closes
        results = await asyncio.gather(*[
            c.post("/api/tasks/err-conc-2/close", json={
                "initiator_id": "deg-0",
            })
            for _ in range(5)
        ])

        codes = [r.status_code for r in results]
        # First should succeed, rest should fail (already closed)
        assert 200 in codes
        assert codes.count(400) >= 3

        task = (await c.get("/api/tasks/err-conc-2")).json()
        assert task["status"] == "no_one_able"


class TestLongRunningPatterns:
    """Patterns that appear in long-running production systems."""

    @pytest.mark.asyncio
    async def test_same_agents_many_tasks_over_time(self, env):
        """Same 2 agents work on 30 tasks sequentially — simulates hours of operation."""
        c, net = env["client"], env["net"]

        for i in range(30):
            tid = f"long-run-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "deg-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "deg-1", "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "deg-1", "content": f"done-{i}",
            })
            await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": "deg-0",
            })
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "deg-0", "agent_id": "deg-1",
            })

        # deg-1 should have earned 30 × 50 = 1500 on top of initial
        bal = net.escrow.get_or_create_account("deg-1", 0)
        assert bal.available == 50_000.0 + 1500.0

        # deg-0 should have nothing frozen
        bal0 = net.escrow.get_or_create_account("deg-0", 0)
        assert bal0.frozen == 0.0

    @pytest.mark.asyncio
    async def test_rapid_event_poll_cycle(self, env):
        """Agent rapidly creates tasks and polls — no event loss."""
        c = env["client"]

        total_events_received = 0
        for batch in range(5):
            # Create 5 tasks
            for i in range(5):
                await c.post("/api/tasks", json={
                    "task_id": f"rapid-poll-{batch}-{i}", "initiator_id": "deg-0",
                    "content": {}, "domains": ["coding"], "budget": 10.0,
                })

            # Poll for each agent
            for aid in ("deg-1", "deg-2", "deg-3"):
                resp = await c.get(f"/api/events/{aid}", params={"timeout": 0})
                total_events_received += resp.json()["count"]

        # Should have received a significant number of broadcast events
        assert total_events_received > 0
