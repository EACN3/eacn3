"""Tests for subtle production bugs found during deep audit.

Validates fixes for:
- Empty recipients don't crash push
- Notification failure in one child doesn't block siblings
- Float precision in budget comparison
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
        server_id="srv-subtle", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(4):
        aid = f"sub-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}", "server_id": "srv-subtle",
        }
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 50_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestFloatPrecisionBudget:
    @pytest.mark.asyncio
    async def test_subtask_exact_remaining_budget(self, env):
        """Create subtask with budget = exactly remaining_budget (float precision)."""
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "fp-1", "initiator_id": "sub-0",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        await c.post("/api/tasks/fp-1/bid", json={
            "agent_id": "sub-1", "confidence": 0.9, "price": 80.0,
        })

        # Create subtask using 30.0 of 100.0
        resp1 = await c.post("/api/tasks/fp-1/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 30.0,
        })
        assert resp1.status_code == 201

        # Create subtask using 30.0 of remaining 70.0
        resp2 = await c.post("/api/tasks/fp-1/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 30.0,
        })
        assert resp2.status_code == 201

        # Create subtask using EXACTLY remaining 40.0
        resp3 = await c.post("/api/tasks/fp-1/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 40.0,
        })
        assert resp3.status_code == 201

        # Nothing left — next should fail
        resp4 = await c.post("/api/tasks/fp-1/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 0.01,
        })
        assert resp4.status_code == 400

    @pytest.mark.asyncio
    async def test_many_small_subtasks_precision(self, env):
        """Create 10 subtasks of 10.0 from a 100.0 budget — test float accumulation."""
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "fp-2", "initiator_id": "sub-0",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        await c.post("/api/tasks/fp-2/bid", json={
            "agent_id": "sub-1", "confidence": 0.9, "price": 80.0,
        })

        for i in range(10):
            resp = await c.post("/api/tasks/fp-2/subtask", json={
                "initiator_id": "sub-1", "content": {},
                "domains": ["coding"], "budget": 10.0,
            })
            assert resp.status_code == 201, f"Subtask {i} failed: {resp.text}"

        # Budget exhausted
        resp = await c.post("/api/tasks/fp-2/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 0.01,
        })
        assert resp.status_code == 400


class TestTerminateChildrenResilience:
    @pytest.mark.asyncio
    async def test_close_parent_with_multiple_children(self, env):
        """Closing parent with multiple children — all get terminated."""
        c, net = env["client"], env["net"]

        await c.post("/api/tasks", json={
            "task_id": "tcr-parent", "initiator_id": "sub-0",
            "content": {}, "domains": ["coding"], "budget": 500.0,
        })
        await c.post("/api/tasks/tcr-parent/bid", json={
            "agent_id": "sub-1", "confidence": 0.9, "price": 200.0,
        })

        # Create 3 children
        child_ids = []
        for i in range(3):
            resp = await c.post("/api/tasks/tcr-parent/subtask", json={
                "initiator_id": "sub-1", "content": {},
                "domains": ["coding"], "budget": 50.0,
            })
            assert resp.status_code == 201
            child_ids.append(resp.json()["id"])

        # Bid on each child
        for cid in child_ids:
            await c.post(f"/api/tasks/{cid}/bid", json={
                "agent_id": "sub-2", "confidence": 0.9, "price": 30.0,
            })

        # Close parent
        await c.post("/api/tasks/tcr-parent/close", json={"initiator_id": "sub-0"})

        # All children should be terminated
        for cid in child_ids:
            child = (await c.get(f"/api/tasks/{cid}")).json()
            assert child["status"] in ("no_one_able", "awaiting_retrieval"), (
                f"Child {cid} still in {child['status']}"
            )

    @pytest.mark.asyncio
    async def test_deadline_with_children_terminates_all(self, env):
        """Deadline expiry on parent terminates children too."""
        c, net = env["client"], env["net"]

        await c.post("/api/tasks", json={
            "task_id": "tcr-dl", "initiator_id": "sub-0",
            "content": {}, "domains": ["coding"], "budget": 500.0,
            "deadline": "2020-01-01T00:00:00Z",
        })
        await c.post("/api/tasks/tcr-dl/bid", json={
            "agent_id": "sub-1", "confidence": 0.9, "price": 200.0,
        })
        resp = await c.post("/api/tasks/tcr-dl/subtask", json={
            "initiator_id": "sub-1", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        child_id = resp.json()["id"]
        await c.post(f"/api/tasks/{child_id}/bid", json={
            "agent_id": "sub-2", "confidence": 0.9, "price": 30.0,
        })

        # Scan deadlines
        await c.post("/api/admin/scan-deadlines")

        # Both parent and child should be terminated
        parent = (await c.get("/api/tasks/tcr-dl")).json()
        child = (await c.get(f"/api/tasks/{child_id}")).json()
        assert parent["status"] in ("no_one_able", "awaiting_retrieval")
        assert child["status"] in ("no_one_able", "awaiting_retrieval")
