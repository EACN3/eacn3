"""Multi-agent lifecycle tests — simulating what happens when multiple agents
are registered, work on tasks, and interact through the full system.

These tests target the exact patterns that fail during real usage:
- Multiple agents from same server
- Agent re-registration after disconnect
- Concurrent task creation + bidding across agents
- Budget exhaustion and recovery
- Discussion + result + select flow with multiple participants
"""

import asyncio
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
    """Full environment with offline store wired."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    store = OfflineStore(db=db)

    import uuid
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}

    await db.close()


async def _register_server(c) -> str:
    resp = await c.post("/api/discovery/servers", json={
        "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
    })
    return resp.json()["server_id"]


async def _register_agent(c, sid: str, agent_id: str, domains: list[str]) -> dict:
    resp = await c.post("/api/discovery/agents", json={
        "agent_id": agent_id, "name": f"Agent {agent_id}",
        "domains": domains,
        "skills": [{"name": "work", "description": "does work"}],
        "url": f"http://localhost:900{hash(agent_id) % 100}",
        "server_id": sid,
    })
    return resp.json()


async def _fund(net, agent_id: str, amount: float):
    net.escrow.get_or_create_account(agent_id, amount)


async def _set_rep(net, agent_id: str, score: float = 0.8):
    net.reputation._scores[agent_id] = score


class TestMultiAgentFromSameServer:
    """Multiple agents registered under the same server."""

    @pytest.mark.asyncio
    async def test_3_agents_from_one_server(self, env):
        c, net = env["client"], env["net"]
        sid = await _register_server(c)

        for i in range(3):
            reg = await _register_agent(c, sid, f"srv-agent-{i}", ["coding"])
            assert "agent_id" in reg
            await _set_rep(net, f"srv-agent-{i}")

        # All should be discoverable
        resp = await c.get("/api/discovery/query", params={"domain": "coding"})
        found = resp.json()["agent_ids"]
        for i in range(3):
            assert f"srv-agent-{i}" in found

    @pytest.mark.asyncio
    async def test_agents_bid_on_same_task(self, env):
        """3 agents from same server all bid on a task created by another agent."""
        c, net = env["client"], env["net"]
        sid = await _register_server(c)

        # Create initiator and 3 workers
        await _register_agent(c, sid, "init-1", ["coding"])
        await _fund(net, "init-1", 5000.0)
        await _set_rep(net, "init-1")

        for i in range(3):
            await _register_agent(c, sid, f"worker-{i}", ["coding"])
            await _set_rep(net, f"worker-{i}")

        # Create task
        resp = await c.post("/api/tasks", json={
            "task_id": "multi-srv-1", "initiator_id": "init-1",
            "content": {"desc": "test"}, "domains": ["coding"],
            "budget": 500.0, "max_concurrent_bidders": 2,
        })
        assert resp.status_code == 201

        # All 3 workers bid
        bid_results = []
        for i in range(3):
            resp = await c.post("/api/tasks/multi-srv-1/bid", json={
                "agent_id": f"worker-{i}", "confidence": 0.9, "price": 80.0,
            })
            assert resp.status_code == 200
            bid_results.append(resp.json())

        # 2 should be executing, 1 waiting
        statuses = [b["status"] for b in bid_results]
        assert statuses.count("executing") == 2
        assert statuses.count("waiting") == 1


class TestAgentReregistration:
    """Agent disconnects and re-registers."""

    @pytest.mark.asyncio
    async def test_agent_reregister_after_unregister(self, env):
        c, net = env["client"], env["net"]
        sid = await _register_server(c)

        # Register
        await _register_agent(c, sid, "rereg-1", ["coding"])

        # Unregister
        resp = await c.delete("/api/discovery/agents/rereg-1")
        assert resp.status_code == 200

        # Re-register with different domains
        reg = await _register_agent(c, sid, "rereg-1", ["coding", "design"])
        assert reg["agent_id"] == "rereg-1"

        # Should be discoverable under both domains
        for domain in ["coding", "design"]:
            resp = await c.get("/api/discovery/query", params={"domain": domain})
            assert "rereg-1" in resp.json()["agent_ids"]


class TestBudgetExhaustion:
    """Test behavior when initiator's balance runs out."""

    @pytest.mark.asyncio
    async def test_create_fails_when_broke(self, env):
        c, net = env["client"], env["net"]
        await _fund(net, "poor-init", 100.0)

        # First task succeeds (100 budget)
        resp = await c.post("/api/tasks", json={
            "task_id": "broke-1", "initiator_id": "poor-init",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 201

        # Second task fails (no remaining balance)
        resp = await c.post("/api/tasks", json={
            "task_id": "broke-2", "initiator_id": "poor-init",
            "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 402

    @pytest.mark.asyncio
    async def test_refund_enables_new_task(self, env):
        """After task refund, initiator can create new tasks."""
        c, net = env["client"], env["net"]
        await _fund(net, "refund-init", 200.0)

        # Create and close (refund)
        await c.post("/api/tasks", json={
            "task_id": "refund-1", "initiator_id": "refund-init",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/refund-1/close", json={"initiator_id": "refund-init"})

        # Should be able to create another task now
        resp = await c.post("/api/tasks", json={
            "task_id": "refund-2", "initiator_id": "refund-init",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        assert resp.status_code == 201


class TestFullCollaborationCycle:
    """Complete collaboration: create → discover → bid → discuss → result → select."""

    @pytest.mark.asyncio
    async def test_full_cycle_with_events(self, env):
        c, net, store = env["client"], env["net"], env["store"]
        sid = await _register_server(c)

        # Setup
        await _register_agent(c, sid, "alice", ["coding"])
        await _register_agent(c, sid, "bob", ["coding"])
        await _fund(net, "alice", 5000.0)
        await _set_rep(net, "alice")
        await _set_rep(net, "bob")

        # 1. Alice creates task
        resp = await c.post("/api/tasks", json={
            "task_id": "collab-1", "initiator_id": "alice",
            "content": {"desc": "Build a REST API"}, "domains": ["coding"],
            "budget": 500.0,
        })
        assert resp.status_code == 201

        # 2. Bob should receive broadcast
        events = await c.get("/api/events/bob", params={"timeout": 0})
        types = [e["type"] for e in events.json()["events"]]
        assert "task_broadcast" in types

        # 3. Bob bids
        resp = await c.post("/api/tasks/collab-1/bid", json={
            "agent_id": "bob", "confidence": 0.9, "price": 300.0,
        })
        assert resp.status_code == 200

        # 4. Bob should get bid_result
        events = await c.get("/api/events/bob", params={"timeout": 0})
        types = [e["type"] for e in events.json()["events"]]
        assert "bid_result" in types

        # 5. Alice adds discussion
        resp = await c.post("/api/tasks/collab-1/discussions", json={
            "initiator_id": "alice",
            "message": "Please use FastAPI",
        })
        assert resp.status_code == 200

        # 6. Bob gets discussion_update
        events = await c.get("/api/events/bob", params={"timeout": 0})
        types = [e["type"] for e in events.json()["events"]]
        assert "discussion_update" in types

        # 7. Bob submits result
        await c.post("/api/tasks/collab-1/result", json={
            "agent_id": "bob",
            "content": {"code": "import fastapi..."},
        })

        # 8. Alice closes and selects
        await c.post("/api/tasks/collab-1/close", json={"initiator_id": "alice"})

        # Clear alice's events first
        await c.get("/api/events/alice", params={"timeout": 0})

        resp = await c.post("/api/tasks/collab-1/select", json={
            "initiator_id": "alice", "agent_id": "bob",
        })
        assert resp.status_code == 200

        # 9. Verify final state
        task = (await c.get("/api/tasks/collab-1")).json()
        assert task["status"] == "completed"

        # Bob should have been paid
        bob_bal = (await c.get("/api/economy/balance",
                              params={"agent_id": "bob"})).json()
        assert bob_bal["available"] == 300.0


class TestConcurrentAgentOperations:
    """Multiple agents doing things at the same time."""

    @pytest.mark.asyncio
    async def test_concurrent_task_creation_by_different_agents(self, env):
        c, net = env["client"], env["net"]

        for i in range(5):
            await _fund(net, f"creator-{i}", 1000.0)

        # 5 agents create tasks concurrently
        results = await asyncio.gather(*[
            c.post("/api/tasks", json={
                "task_id": f"conc-create-{i}", "initiator_id": f"creator-{i}",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            for i in range(5)
        ])

        assert all(r.status_code == 201 for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_event_polling(self, env):
        """Multiple agents polling events concurrently."""
        c, net = env["client"], env["net"]
        sid = await _register_server(c)

        for i in range(5):
            await _register_agent(c, sid, f"poller-{i}", ["coding"])
            await _set_rep(net, f"poller-{i}")
        await _fund(net, "poll-init", 5000.0)

        # Create task to generate broadcasts
        await c.post("/api/tasks", json={
            "task_id": "poll-task", "initiator_id": "poll-init",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })

        # All 5 agents poll concurrently
        results = await asyncio.gather(*[
            c.get(f"/api/events/poller-{i}", params={"timeout": 0})
            for i in range(5)
        ])

        # Each should have gotten the broadcast
        for i, resp in enumerate(results):
            assert resp.status_code == 200
            types = [e["type"] for e in resp.json()["events"]]
            assert "task_broadcast" in types, f"poller-{i} missing broadcast: {types}"


class TestEdgeCaseScenarios:
    """Edge cases from real usage."""

    @pytest.mark.asyncio
    async def test_bid_on_own_task(self, env):
        """Initiator bids on their own task — should work (self-execution)."""
        c, net = env["client"], env["net"]
        sid = await _register_server(c)
        await _register_agent(c, sid, "self-exec", ["coding"])
        await _fund(net, "self-exec", 5000.0)
        await _set_rep(net, "self-exec")

        await c.post("/api/tasks", json={
            "task_id": "self-1", "initiator_id": "self-exec",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        resp = await c.post("/api/tasks/self-1/bid", json={
            "agent_id": "self-exec", "confidence": 0.9, "price": 100.0,
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_close_already_completed_task(self, env):
        """Closing an already-completed task should fail gracefully."""
        c, net = env["client"], env["net"]
        sid = await _register_server(c)
        await _register_agent(c, sid, "closer", ["coding"])
        await _fund(net, "closer-init", 5000.0)
        await _set_rep(net, "closer")

        await c.post("/api/tasks", json={
            "task_id": "close-comp", "initiator_id": "closer-init",
            "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        await c.post("/api/tasks/close-comp/bid", json={
            "agent_id": "closer", "confidence": 0.9, "price": 80.0,
        })
        await c.post("/api/tasks/close-comp/result", json={
            "agent_id": "closer", "content": "done",
        })
        await c.post("/api/tasks/close-comp/close", json={"initiator_id": "closer-init"})
        await c.post("/api/tasks/close-comp/select", json={
            "initiator_id": "closer-init", "agent_id": "closer",
        })

        # Already completed — close should fail
        resp = await c.post("/api/tasks/close-comp/close", json={"initiator_id": "closer-init"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_submit_result_to_nonexistent_task(self, env):
        c = env["client"]
        resp = await c.post("/api/tasks/nonexistent/result", json={
            "agent_id": "a1", "content": "result",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, env):
        c = env["client"]
        resp = await c.get("/api/tasks/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deposit_and_create_task(self, env):
        """Deposit funds then immediately create a task."""
        c = env["client"]
        # Deposit
        resp = await c.post("/api/economy/deposit", json={
            "agent_id": "depositor", "amount": 500.0,
        })
        assert resp.status_code == 200

        # Create task with deposited funds
        resp = await c.post("/api/tasks", json={
            "task_id": "dep-task", "initiator_id": "depositor",
            "content": {}, "domains": ["coding"], "budget": 300.0,
        })
        assert resp.status_code == 201

        # Check balance: 200 available, 300 frozen
        bal = (await c.get("/api/economy/balance",
                          params={"agent_id": "depositor"})).json()
        assert bal["available"] == 200.0
        assert bal["frozen"] == 300.0
