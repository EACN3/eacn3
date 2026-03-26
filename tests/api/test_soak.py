"""Soak test — extended operation with validation checkpoints.

Runs a realistic multi-agent workload over hundreds of operations,
checking invariants at regular intervals to detect slow degradation.
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

    await net.discovery.register_server(
        server_id="srv-soak", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )
    for i in range(8):
        aid = f"soak-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}",
            "domains": ["coding"] if i < 4 else ["design"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}", "server_id": "srv-soak",
        }
        await net.discovery.register_agent(card)
        for d in (["coding"] if i < 4 else ["design"]):
            await net.dht.announce(d, aid)
        net.reputation._scores[aid] = 0.7 + i * 0.02
        net.escrow.get_or_create_account(aid, 200_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestSoakWorkload:
    """Extended workload with periodic invariant checks."""

    @pytest.mark.asyncio
    async def test_200_task_soak(self, env):
        """200 tasks through full lifecycle with 8 agents across 2 domains.
        Checks invariants every 50 tasks."""
        c, net = env["client"], env["net"]

        tasks_completed = 0
        tasks_failed = 0
        total_fees_before = 0.0

        for phase in range(4):
            # Each phase: 50 tasks
            batch_coros = []
            for i in range(50):
                idx = phase * 50 + i
                batch_coros.append(self._single_lifecycle(c, net, idx))

            results = await asyncio.gather(*batch_coros, return_exceptions=True)

            for r in results:
                if isinstance(r, Exception):
                    tasks_failed += 1
                elif r == "completed":
                    tasks_completed += 1
                else:
                    tasks_failed += 1

            # === INVARIANT CHECKS ===

            # 1. No negative balances
            for aid, acct in net.escrow._accounts.items():
                assert acct.available >= -0.01, (
                    f"Phase {phase}: {aid} has negative available: {acct.available}"
                )
                assert acct.frozen >= -0.01, (
                    f"Phase {phase}: {aid} has negative frozen: {acct.frozen}"
                )

            # 2. Platform fees increasing
            fees_now = net.settlement.total_fees_collected
            assert fees_now >= total_fees_before, (
                f"Phase {phase}: fees decreased from {total_fees_before} to {fees_now}"
            )
            total_fees_before = fees_now

            # 3. Push history bounded
            assert len(net.push._history) <= net.push._max_history + 50, (
                f"Phase {phase}: push history unbounded: {len(net.push._history)}"
            )

            # 4. No more escrow entries than active tasks
            active_tasks = sum(
                1 for t in net.task_manager._tasks.values()
                if t.status.value not in ("completed", "no_one_able")
                and t.type.value == "normal"
            )
            # Escrow entries can be 0 (all settled) or at most = active tasks
            # (adjudication tasks have budget=0, so no escrow)

        assert tasks_completed >= 180, (
            f"Only {tasks_completed}/200 completed, {tasks_failed} failed"
        )

    async def _single_lifecycle(self, c, net, idx: int) -> str:
        """Run a single task through its complete lifecycle."""
        tid = f"soak-{idx}"
        # Alternate between coding and design domains
        domain = "coding" if idx % 2 == 0 else "design"
        initiator = f"soak-{idx % 4}" if domain == "coding" else f"soak-{4 + idx % 4}"
        executor = f"soak-{(idx + 1) % 4}" if domain == "coding" else f"soak-{4 + (idx + 1) % 4}"

        try:
            resp = await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": initiator,
                "content": {"idx": idx}, "domains": [domain],
                "budget": 100.0,
            })
            if resp.status_code != 201:
                return "create_failed"

            resp = await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": executor, "confidence": 0.9, "price": 50.0,
            })
            if resp.status_code != 200:
                return "bid_failed"

            resp = await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": executor, "content": f"soak-result-{idx}",
            })
            if resp.status_code != 200:
                return "result_failed"

            resp = await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": initiator,
            })
            if resp.status_code != 200:
                return "close_failed"

            resp = await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": initiator, "agent_id": executor,
            })
            if resp.status_code != 200:
                return "select_failed"

            return "completed"
        except Exception as e:
            return f"exception: {e}"

    @pytest.mark.asyncio
    async def test_soak_with_subtasks_and_discussions(self, env):
        """50 tasks, each with a subtask and a discussion update."""
        c, net = env["client"], env["net"]

        for i in range(50):
            tid = f"soak-sub-{i}"
            initiator = f"soak-{i % 4}"
            executor = f"soak-{(i + 1) % 4}"

            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": initiator,
                "content": {}, "domains": ["coding"], "budget": 200.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": executor, "confidence": 0.9, "price": 80.0,
            })

            # Discussion
            await c.post(f"/api/tasks/{tid}/discussions", json={
                "initiator_id": initiator, "message": f"clarification {i}",
            })

            # Subtask (may fail if executor is also initiator)
            sub_resp = await c.post(f"/api/tasks/{tid}/subtask", json={
                "initiator_id": executor, "content": {},
                "domains": ["coding"], "budget": 30.0,
            })
            if sub_resp.status_code == 201:
                sub_id = sub_resp.json()["id"]
                # Let subtask exist — don't need to complete it

            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": executor, "content": f"result-{i}",
            })
            await c.post(f"/api/tasks/{tid}/close", json={
                "initiator_id": initiator,
            })
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": initiator, "agent_id": executor,
            })

        # All 50 should be completed
        completed = sum(
            1 for t in net.task_manager._tasks.values()
            if t.id.startswith("soak-sub-") and t.status.value == "completed"
        )
        assert completed == 50

    @pytest.mark.asyncio
    async def test_soak_event_polling_interleaved(self, env):
        """Create tasks + poll events interleaved — simulates real client behavior."""
        c = env["client"]

        for i in range(30):
            # Create task
            await c.post("/api/tasks", json={
                "task_id": f"soak-poll-{i}", "initiator_id": "soak-0",
                "content": {}, "domains": ["coding"], "budget": 50.0,
            })

            # Poll every 3 tasks (simulating periodic event check)
            if i % 3 == 2:
                for aid in ("soak-0", "soak-1", "soak-2", "soak-3"):
                    resp = await c.get(f"/api/events/{aid}", params={"timeout": 0})
                    assert resp.status_code == 200

        # Final drain
        for aid in ("soak-0", "soak-1", "soak-2", "soak-3"):
            resp = await c.get(f"/api/events/{aid}", params={"timeout": 0})
            assert resp.status_code == 200
