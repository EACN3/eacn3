"""Adjudication workflow tests.

Tests the full adjudication lifecycle:
- Normal task result triggers adjudication task creation
- Adjudication tasks don't spawn further adjudications (recursion termination)
- Adjudication results attach to parent task's result
- Duplicate adjudicator submission is idempotent
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
    store = OfflineStore(db=db)
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
        server_id="srv-adj", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(5):
        aid = f"adj-{i}"
        card = {"agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
                "skills": [{"name": "work", "description": "work"}],
                "url": f"http://localhost:900{i}", "server_id": "srv-adj"}
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 50_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestAdjudicationCreation:
    @pytest.mark.asyncio
    async def test_result_triggers_adjudication_task(self, env):
        """Submitting a result to a normal task creates an adjudication task."""
        c, net = env["client"], env["net"]

        await c.post("/api/tasks", json={
            "task_id": "adj-parent", "initiator_id": "adj-0",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/adj-parent/bid", json={
            "agent_id": "adj-1", "confidence": 0.9, "price": 80.0,
        })
        await c.post("/api/tasks/adj-parent/result", json={
            "agent_id": "adj-1", "content": {"answer": "42"},
        })

        # Check adjudication tasks were created
        all_tasks = net.task_manager.list_all()
        adj_tasks = [t for t in all_tasks if t.type.value == "adjudication"]
        assert len(adj_tasks) >= 1

        # Adjudication task should reference the parent
        adj = adj_tasks[0]
        assert adj.parent_id == "adj-parent"
        assert adj.budget == 0.0  # No monetary compensation

    @pytest.mark.asyncio
    async def test_adjudication_task_no_recursion(self, env):
        """Adjudication tasks don't spawn further adjudication tasks."""
        c, net = env["client"], env["net"]

        await c.post("/api/tasks", json={
            "task_id": "adj-norecurse", "initiator_id": "adj-0",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/adj-norecurse/bid", json={
            "agent_id": "adj-1", "confidence": 0.9, "price": 80.0,
        })
        await c.post("/api/tasks/adj-norecurse/result", json={
            "agent_id": "adj-1", "content": "result",
        })

        # Get the adjudication task
        adj_tasks = [t for t in net.task_manager.list_all()
                     if t.type.value == "adjudication" and t.parent_id == "adj-norecurse"]
        assert len(adj_tasks) >= 1

        # Bid on adjudication task
        adj_id = adj_tasks[0].id
        await c.post(f"/api/tasks/{adj_id}/bid", json={
            "agent_id": "adj-2", "confidence": 0.9, "price": 0.0,
        })
        # Submit adjudication result
        await c.post(f"/api/tasks/{adj_id}/result", json={
            "agent_id": "adj-2", "content": {"verdict": "good", "score": 0.9},
        })

        # No new adjudication tasks should have been created for the adjudication task
        adj_of_adj = [t for t in net.task_manager.list_all()
                      if t.type.value == "adjudication" and t.parent_id == adj_id]
        assert len(adj_of_adj) == 0, "Adjudication spawned further adjudication!"


class TestAdjudicationUnit:
    @pytest.mark.asyncio
    async def test_adjudication_service_methods(self, env):
        """Test adjudication service in isolation."""
        net = env["net"]
        from eacn.core.models import Task, TaskType, TaskStatus, Result

        parent = Task(
            id="test-parent", content={}, initiator_id="user",
            domains=["coding"], budget=100.0,
        )
        parent.results.append(Result(agent_id="exec-1", content="result"))

        # should_create returns True for normal tasks
        assert net.adjudication.should_create_adjudication(parent)

        # Create adjudication task
        adj_task = net.adjudication.create_adjudication_task(parent, "exec-1")
        assert adj_task.type == TaskType.ADJUDICATION
        assert adj_task.budget == 0.0
        assert adj_task.parent_id == "test-parent"

        # should_create returns False for adjudication tasks
        assert not net.adjudication.should_create_adjudication(adj_task)

    @pytest.mark.asyncio
    async def test_collect_adjudication_idempotent(self, env):
        """Duplicate adjudicator submission is skipped (#11)."""
        net = env["net"]
        from eacn.core.models import Task, Result

        parent = Task(
            id="idem-parent", content={}, initiator_id="user",
            domains=["coding"], budget=100.0,
        )
        parent.results.append(Result(agent_id="exec-1", content="result"))

        # First adjudication
        net.adjudication.collect_adjudication_result(
            parent, "exec-1", "judge-1", "approved", 0.9,
        )
        assert len(parent.results[0].adjudications) == 1

        # Duplicate — should be skipped
        net.adjudication.collect_adjudication_result(
            parent, "exec-1", "judge-1", "approved again", 0.95,
        )
        assert len(parent.results[0].adjudications) == 1  # Still 1

        # Different adjudicator — should succeed
        net.adjudication.collect_adjudication_result(
            parent, "exec-1", "judge-2", "also approved", 0.85,
        )
        assert len(parent.results[0].adjudications) == 2

    @pytest.mark.asyncio
    async def test_compute_summary(self, env):
        net = env["net"]
        from eacn.core.models import Result, Adjudication

        result = Result(agent_id="exec", content="test")
        result.adjudications = [
            Adjudication(adjudicator_id="j1", verdict="good", score=0.8),
            Adjudication(adjudicator_id="j2", verdict="ok", score=0.6),
            Adjudication(adjudicator_id="j3", verdict="great", score=1.0),
        ]

        summary = net.adjudication.compute_adjudication_summary(result)
        assert summary["count"] == 3
        assert abs(summary["avg_score"] - 0.8) < 0.01
        assert summary["min_score"] == 0.6
        assert summary["max_score"] == 1.0
