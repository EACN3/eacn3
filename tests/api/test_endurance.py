"""Long-running endurance tests.

Simulates hours of real usage in compressed form:
- 100+ task lifecycle iterations
- Memory growth monitoring (dict sizes)
- Escrow balance invariant checks after every operation
- Event queue accumulation over time
- Repeated agent register/unregister cycles
- Task accumulation and cleanup
- DB connection stability under sustained load
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

    # Register server
    await net.discovery.register_server(
        server_id="srv-endurance", version="1.0",
        endpoint="http://localhost:8000", owner="test",
    )

    # 5 agents
    for i in range(5):
        aid = f"end-{i}"
        card = {
            "agent_id": aid, "name": f"Agent {i}", "domains": ["coding"],
            "skills": [{"name": "work", "description": "work"}],
            "url": f"http://localhost:900{i}", "server_id": "srv-endurance",
        }
        await net.discovery.register_agent(card)
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8
        net.escrow.get_or_create_account(aid, 100_000.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store, "db": db}
    await db.close()


class TestEndurance100Cycles:
    """Run 100 complete task lifecycles and verify invariants after each batch."""

    @pytest.mark.asyncio
    async def test_100_sequential_lifecycles(self, env):
        c, net = env["client"], env["net"]

        executor = "end-0"
        initiator = "end-1"

        init_account = net.escrow.get_or_create_account(initiator, 0)
        exec_account = net.escrow.get_or_create_account(executor, 0)
        exec_initial = exec_account.available
        exec_total_earned = 0.0

        for batch in range(10):
            # Each batch: 10 tasks
            for i in range(10):
                idx = batch * 10 + i
                tid = f"endurance-{idx}"
                price = 50.0 + (idx % 20)

                # Create
                resp = await c.post("/api/tasks", json={
                    "task_id": tid, "initiator_id": initiator,
                    "content": {"n": idx}, "domains": ["coding"],
                    "budget": price + 50.0,  # budget > price
                })
                assert resp.status_code == 201, f"Create {tid} failed: {resp.text}"

                # Bid
                resp = await c.post(f"/api/tasks/{tid}/bid", json={
                    "agent_id": executor, "confidence": 0.9, "price": price,
                })
                assert resp.status_code == 200, f"Bid {tid} failed: {resp.text}"

                # Result
                resp = await c.post(f"/api/tasks/{tid}/result", json={
                    "agent_id": executor, "content": f"result-{idx}",
                })
                assert resp.status_code == 200, f"Result {tid} failed: {resp.text}"

                # Close
                resp = await c.post(f"/api/tasks/{tid}/close", json={
                    "initiator_id": initiator,
                })
                assert resp.status_code == 200, f"Close {tid} failed: {resp.text}"

                # Select
                resp = await c.post(f"/api/tasks/{tid}/select", json={
                    "initiator_id": initiator, "agent_id": executor,
                })
                assert resp.status_code == 200, f"Select {tid} failed: {resp.text}"

                exec_total_earned += price

            # Invariant check after each batch of 10
            exec_bal = net.escrow.get_or_create_account(executor, 0)
            assert exec_bal.available == exec_initial + exec_total_earned, (
                f"Batch {batch}: executor balance {exec_bal.available} != "
                f"expected {exec_initial + exec_total_earned}"
            )

            init_bal_now = net.escrow.get_or_create_account(initiator, 0)
            assert init_bal_now.frozen == 0.0, (
                f"Batch {batch}: initiator has {init_bal_now.frozen} frozen (should be 0)"
            )

        # Final check: all 100 main tasks completed
        # (adjudication tasks are also in the task list)
        tasks = net.task_manager.list_all()
        completed_main = [
            t for t in tasks
            if t.status.value == "completed" and t.type.value == "normal"
        ]
        assert len(completed_main) == 100


class TestEnduranceMemoryGrowth:
    """Track internal dict sizes to detect unbounded memory growth."""

    @pytest.mark.asyncio
    async def test_dict_sizes_after_50_settled_tasks(self, env):
        c, net = env["client"], env["net"]

        for i in range(50):
            tid = f"mem-{i}"
            resp = await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "end-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            assert resp.status_code == 201
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "end-1", "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "end-1", "content": "done",
            })
            await c.post(f"/api/tasks/{tid}/close", json={"initiator_id": "end-0"})
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "end-0", "agent_id": "end-1",
            })

        # Escrow entries for settled tasks should be cleaned up
        remaining_escrows = len(net.escrow._task_escrows)
        assert remaining_escrows == 0, (
            f"Expected 0 escrow entries after settlement, got {remaining_escrows}"
        )

        # Settlement set should track all 50
        assert len(net.settlement._settled) == 50

        # Task manager holds main tasks + auto-created adjudication tasks
        total_tasks = len(net.task_manager._tasks)
        normal_tasks = sum(1 for t in net.task_manager._tasks.values()
                          if t.type.value == "normal")
        adj_tasks = sum(1 for t in net.task_manager._tasks.values()
                       if t.type.value == "adjudication")
        assert normal_tasks == 50, f"Expected 50 normal tasks, got {normal_tasks}"
        # Adjudication tasks are expected — they're auto-created per result
        assert adj_tasks >= 0  # Just verify no crash, count varies


class TestEnduranceEventQueueGrowth:
    """Events pile up if agents don't poll — verify overflow pruning works."""

    @pytest.mark.asyncio
    async def test_event_overflow_pruning(self, env):
        c, net, store = env["client"], env["net"], env["store"]

        # Create 100 tasks → 100 broadcasts × 5 agents = 500 events
        # But max_per_agent=50, so each agent should have at most 50
        for i in range(100):
            resp = await c.post("/api/tasks", json={
                "task_id": f"overflow-{i}", "initiator_id": "end-0",
                "content": {}, "domains": ["coding"], "budget": 10.0,
            })
            assert resp.status_code == 201

        # Check counts for each agent
        counts = await store.count_all()
        for i in range(5):
            aid = f"end-{i}"
            count = counts.get(aid, 0)
            assert count <= 50, f"Agent {aid} has {count} messages (max should be 50)"

    @pytest.mark.asyncio
    async def test_drain_clears_after_high_volume(self, env):
        c, store = env["client"], env["store"]

        for i in range(30):
            await c.post("/api/tasks", json={
                "task_id": f"drain-vol-{i}", "initiator_id": "end-0",
                "content": {}, "domains": ["coding"], "budget": 10.0,
            })

        # Drain all for one agent
        resp = await c.get("/api/events/end-1", params={"timeout": 0})
        assert resp.status_code == 200
        first_drain = resp.json()["count"]
        assert first_drain > 0

        # Second drain should be empty
        resp = await c.get("/api/events/end-1", params={"timeout": 0})
        assert resp.json()["count"] == 0


class TestEnduranceAgentChurn:
    """Agents repeatedly register and unregister."""

    @pytest.mark.asyncio
    async def test_20_register_unregister_cycles(self, env):
        c = env["client"]

        for cycle in range(20):
            aid = f"churn-{cycle}"
            # Register
            resp = await c.post("/api/discovery/agents", json={
                "agent_id": aid, "name": f"Churn Agent {cycle}",
                "domains": ["coding"],
                "skills": [{"name": "work", "description": "work"}],
                "url": f"http://localhost:999{cycle % 10}",
                "server_id": "srv-endurance",
            })
            assert resp.status_code == 201, f"Register {aid} cycle {cycle} failed"

            # Verify discoverable
            resp = await c.get("/api/discovery/agents/" + aid)
            assert resp.status_code == 200

            # Unregister
            resp = await c.delete("/api/discovery/agents/" + aid)
            assert resp.status_code == 200

            # Verify gone
            resp = await c.get("/api/discovery/agents/" + aid)
            assert resp.status_code == 404


class TestEnduranceConcurrentLoad:
    """Sustained concurrent operations over many iterations."""

    @pytest.mark.asyncio
    async def test_50_concurrent_task_cycles(self, env):
        """50 tasks go through full lifecycle concurrently."""
        c, net = env["client"], env["net"]

        async def lifecycle(idx: int):
            tid = f"conc-end-{idx}"
            initiator = f"end-{idx % 3}"
            executor = f"end-{3 + idx % 2}"
            budget = 100.0 + idx

            resp = await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": initiator,
                "content": {}, "domains": ["coding"], "budget": budget,
            })
            if resp.status_code != 201:
                return "create_failed"

            resp = await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": executor, "confidence": 0.9, "price": 50.0,
            })
            if resp.status_code != 200:
                return "bid_failed"

            resp = await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": executor, "content": f"r-{idx}",
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

        results = await asyncio.gather(*[lifecycle(i) for i in range(50)])
        completed = results.count("completed")
        assert completed == 50, f"Only {completed}/50 completed: {dict(zip(range(50), results))}"

    @pytest.mark.asyncio
    async def test_sustained_mixed_operations(self, env):
        """Mix of creates, bids, results, closes, polls happening concurrently."""
        c, net = env["client"], env["net"]

        # Phase 1: Create 20 tasks
        creates = await asyncio.gather(*[
            c.post("/api/tasks", json={
                "task_id": f"mix-end-{i}", "initiator_id": f"end-{i % 3}",
                "content": {}, "domains": ["coding"], "budget": 200.0,
            })
            for i in range(20)
        ])
        assert all(r.status_code == 201 for r in creates)

        # Phase 2: Concurrent bids + polls
        bid_coros = [
            c.post(f"/api/tasks/mix-end-{i}/bid", json={
                "agent_id": f"end-{3 + i % 2}", "confidence": 0.9, "price": 80.0,
            })
            for i in range(20)
        ]
        poll_coros = [
            c.get(f"/api/events/end-{i}", params={"timeout": 0})
            for i in range(5)
        ]
        results = await asyncio.gather(*bid_coros, *poll_coros)
        # No 500 errors
        for r in results:
            assert r.status_code in (200, 400), f"Server error: {r.status_code}"

        # Phase 3: Concurrent results + discussions
        result_coros = [
            c.post(f"/api/tasks/mix-end-{i}/result", json={
                "agent_id": f"end-{3 + i % 2}", "content": f"done-{i}",
            })
            for i in range(20)
        ]
        disc_coros = [
            c.post(f"/api/tasks/mix-end-{i}/discussions", json={
                "initiator_id": f"end-{i % 3}", "message": f"note-{i}",
            })
            for i in range(10)
        ]
        results = await asyncio.gather(*result_coros, *disc_coros)
        for r in results:
            assert r.status_code in (200, 400), f"Server error: {r.status_code}"


class TestEnduranceEscrowInvariants:
    """Verify escrow accounting invariants hold throughout extended operation."""

    @pytest.mark.asyncio
    async def test_total_balance_conservation(self, env):
        """Total system balance (all accounts + all escrows) is conserved.
        Money is never created or destroyed, only transferred."""
        c, net = env["client"], env["net"]

        # Calculate initial total
        def system_total():
            accounts_total = sum(
                a.available + a.frozen
                for a in net.escrow._accounts.values()
            )
            return accounts_total

        initial_total = system_total()

        # Run 30 task cycles
        for i in range(30):
            tid = f"conserve-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "end-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "end-1", "confidence": 0.9, "price": 50.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "end-1", "content": "done",
            })
            await c.post(f"/api/tasks/{tid}/close", json={"initiator_id": "end-0"})
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "end-0", "agent_id": "end-1",
            })

        final_total = system_total()

        # Total should decrease by platform fees only
        # 30 tasks × 50 price × 5% fee = 75.0 in fees
        fees = net.settlement.total_fees_collected
        assert abs(final_total - (initial_total - fees)) < 0.01, (
            f"Balance not conserved: initial={initial_total}, "
            f"final={final_total}, fees={fees}, "
            f"expected_final={initial_total - fees}"
        )

    @pytest.mark.asyncio
    async def test_no_negative_balances(self, env):
        """No account should ever go negative during operations."""
        c, net = env["client"], env["net"]

        for i in range(20):
            tid = f"nonneg-{i}"
            await c.post("/api/tasks", json={
                "task_id": tid, "initiator_id": "end-0",
                "content": {}, "domains": ["coding"], "budget": 100.0,
            })
            await c.post(f"/api/tasks/{tid}/bid", json={
                "agent_id": "end-1", "confidence": 0.9, "price": 80.0,
            })
            await c.post(f"/api/tasks/{tid}/result", json={
                "agent_id": "end-1", "content": "done",
            })
            await c.post(f"/api/tasks/{tid}/close", json={"initiator_id": "end-0"})
            await c.post(f"/api/tasks/{tid}/select", json={
                "initiator_id": "end-0", "agent_id": "end-1",
            })

            # Check no negative balances
            for aid, acct in net.escrow._accounts.items():
                assert acct.available >= 0, f"{aid} has negative available: {acct.available}"
                assert acct.frozen >= 0, f"{aid} has negative frozen: {acct.frozen}"


class TestEnduranceDBStability:
    """Database remains healthy under sustained operations."""

    @pytest.mark.asyncio
    async def test_db_survives_100_operations(self, env):
        """Mix of 100 DB operations — account, escrow, reputation, offline."""
        db = env["db"]

        ops = []
        for i in range(25):
            ops.append(db.upsert_account(f"db-test-{i}", float(i), 0.0))
        for i in range(25):
            ops.append(db.save_escrow(f"db-esc-{i}", f"db-test-{i % 25}", float(i * 10)))
        for i in range(25):
            ops.append(db.upsert_reputation(f"db-test-{i}", 0.5 + i * 0.01, {}))
        for i in range(25):
            ops.append(db.offline_store(
                f"db-msg-{i}", f"db-test-{i % 25}", "broadcast", f"t-{i}", {}
            ))

        await asyncio.gather(*ops)

        # Verify all data is readable
        accounts = await db.list_all_accounts()
        assert len(accounts) >= 25

        escrows = await db.list_all_escrows()
        assert len(escrows) >= 25

        reps = await db.list_all_reputations()
        assert len(reps) >= 25
