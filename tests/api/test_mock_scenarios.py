"""Tests: Large-scale mock data scenarios.

Validate complex interactions with pre-populated agent/task sets.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task, select_result

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.api.routes import router as net_router, set_network


@pytest.fixture
async def large_network():
    """30 agents, 3 domains, tiered reputation."""
    from eacn3.network.db import Database
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    net.escrow.get_or_create_account("user1", 100_000.0)
    net.escrow.get_or_create_account("user2", 50_000.0)

    # coding: 10 agents (rep 0.5~0.95)
    for i in range(10):
        aid = f"code-{i}"
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.5 + i * 0.05

    # design: 10 agents (rep 0.4~0.85)
    for i in range(10):
        aid = f"design-{i}"
        await net.dht.announce("design", aid)
        net.reputation._scores[aid] = 0.4 + i * 0.05

    # research: 10 agents (rep 0.3~0.75)
    for i in range(10):
        aid = f"research-{i}"
        await net.dht.announce("research", aid)
        net.reputation._scores[aid] = 0.3 + i * 0.05

    yield net
    await db.close()


@pytest.fixture
async def large_client(large_network):
    app = FastAPI()
    app.include_router(net_router)
    set_network(large_network)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestMassiveBidding:
    @pytest.mark.asyncio
    async def test_10_agents_bid_on_same_task(self, large_client):
        await create_task(
            large_client, task_id="popular",
            budget=1000.0, max_concurrent_bidders=3,
        )
        results = []
        for i in range(10):
            b = await bid(
                large_client, task_id="popular",
                agent_id=f"code-{i}", confidence=0.8, price=80.0 + i * 5,
            )
            results.append(b)

        statuses = [r["status"] for r in results]
        assert statuses.count("executing") == 3
        # Rest are either waiting or rejected (low reputation rejected)
        assert statuses.count("executing") + statuses.count("waiting") + statuses.count("rejected") == 10

    @pytest.mark.asyncio
    async def test_varied_reputation_affects_ranking(self, large_client, large_network):
        """High reputation agent should pass, low reputation rejected."""
        await create_task(large_client, task_id="t1", budget=200.0)

        # High reputation (0.95) + high confidence -> pass
        b_high = await bid(large_client, task_id="t1", agent_id="code-9", confidence=0.8, price=80.0)
        assert b_high["status"] == "executing"

        # Low reputation (0.3) + low confidence -> fail
        b_low = await bid(large_client, task_id="t1", agent_id="research-0", confidence=0.3, price=80.0)
        assert b_low["status"] == "rejected"


class TestMultiTaskConcurrency:
    @pytest.mark.asyncio
    async def test_5_tasks_independent_state(self, large_client):
        """5 tasks run independently."""
        for i in range(5):
            await create_task(
                large_client, task_id=f"task-{i}",
                budget=500.0, domains=["coding"],
            )
            # Use high-reputation agent to ensure pass
            await bid(large_client, task_id=f"task-{i}", agent_id=f"code-{5+i}", price=100.0)

        for i in range(5):
            data = (await large_client.get(f"/api/tasks/task-{i}")).json()
            assert data["status"] == "bidding"
            assert len(data["bids"]) >= 1

    @pytest.mark.asyncio
    async def test_agent_bids_on_multiple_tasks(self, large_client):
        """Same agent bids on multiple tasks."""
        for i in range(3):
            await create_task(large_client, task_id=f"multi-{i}", budget=200.0)
            await bid(large_client, task_id=f"multi-{i}", agent_id="code-9", price=80.0)

        for i in range(3):
            data = (await large_client.get(f"/api/tasks/multi-{i}")).json()
            assert any(b["agent_id"] == "code-9" for b in data["bids"])


class TestComplexSubtaskTree:
    @pytest.mark.asyncio
    async def test_3_level_subtask_tree(self, large_client):
        """Three-level subtask tree: parent -> child1, child2 -> grandchild."""
        await create_task(large_client, task_id="root", budget=5000.0, max_depth=5)

        # Must bid on root to create subtasks
        await bid(large_client, task_id="root", agent_id="code-1", price=100.0)
        await bid(large_client, task_id="root", agent_id="code-2", price=100.0)

        # Level 1
        child1 = (await large_client.post("/api/tasks/root/subtask", json={
            "initiator_id": "code-1", "content": {"desc": "frontend"},
            "domains": ["coding"], "budget": 2000.0,
        })).json()
        child2 = (await large_client.post("/api/tasks/root/subtask", json={
            "initiator_id": "code-2", "content": {"desc": "design"},
            "domains": ["design"], "budget": 1000.0,
        })).json()

        # Level 2 — bid on child1 first
        await bid(large_client, task_id=child1["id"], agent_id="code-3", price=50.0)
        grandchild = (await large_client.post(f"/api/tasks/{child1['id']}/subtask", json={
            "initiator_id": "code-3", "content": {"desc": "testing"},
            "domains": ["coding"], "budget": 500.0,
        })).json()

        # Verify structure
        root = (await large_client.get("/api/tasks/root")).json()
        assert len(root["child_ids"]) == 2
        assert root["remaining_budget"] == 2000.0  # 5000 - 2000 - 1000

        c1 = (await large_client.get(f"/api/tasks/{child1['id']}")).json()
        assert len(c1["child_ids"]) == 1
        assert c1["remaining_budget"] == 1500.0  # 2000 - 500
        assert c1["depth"] == 1

        gc = (await large_client.get(f"/api/tasks/{grandchild['id']}")).json()
        assert gc["depth"] == 2
        assert gc["parent_id"] == child1["id"]


class TestEndToEndSettlement:
    @pytest.mark.asyncio
    async def test_full_settlement_releases_funds(self, large_client):
        """After settlement, funds should be released; can create new tasks."""
        # Freeze 80000
        await create_task(large_client, task_id="t1", budget=80000.0)
        await bid(large_client, task_id="t1", agent_id="code-9", price=50000.0)
        await submit_result(large_client, task_id="t1", agent_id="code-9")
        await close_task(large_client, task_id="t1")
        await select_result(large_client, task_id="t1", agent_id="code-9")

        # After settlement, should be able to create new task (balance = 100000 - 50000 - fee + refund)
        resp = await large_client.post("/api/tasks", json={
            "task_id": "t2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 40000.0,
        })
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_refund_on_close(self, large_client):
        """Closing a task with no results refunds full budget."""
        await create_task(large_client, task_id="t1", budget=90000.0)
        await close_task(large_client, task_id="t1")

        # After refund, should be able to use full amount again
        resp = await large_client.post("/api/tasks", json={
            "task_id": "t2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 95000.0,
        })
        assert resp.status_code == 201


class TestReputationProgression:
    @pytest.mark.asyncio
    async def test_reputation_increases_through_tasks(self, large_client):
        """Reputation should keep increasing after completing multiple tasks."""
        initial = (await large_client.get("/api/reputation/code-5")).json()["score"]

        for i in range(3):
            await create_task(large_client, task_id=f"rep-{i}", budget=200.0)
            await bid(large_client, task_id=f"rep-{i}", agent_id="code-5", price=100.0)
            await submit_result(large_client, task_id=f"rep-{i}", agent_id="code-5")
            await close_task(large_client, task_id=f"rep-{i}")
            await select_result(large_client, task_id=f"rep-{i}", agent_id="code-5")

        final = (await large_client.get("/api/reputation/code-5")).json()["score"]
        assert final > initial
