"""Multi-agent concurrent stress tests.

These tests simulate realistic concurrent scenarios that arise when multiple
agents interact with the same tasks simultaneously. They exercise:

- Concurrent bid submission (#13, #29, #113)
- Concurrent select_result (#21, #52)
- Concurrent result submission (#22)
- Concurrent subtask creation budget consistency
- Concurrent bid + close race
- Concurrent bid + deadline expiry race
- Mixed workload: N agents × M tasks simultaneously (#114)

All tests use asyncio.gather to create actual concurrent execution,
verifying that locks and guards prevent inconsistencies.
"""

import asyncio

import pytest

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════

async def _get_task(client, task_id: str) -> dict:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def _get_balance(client, agent_id: str) -> dict:
    return (await client.get("/api/economy/balance", params={"agent_id": agent_id})).json()


async def _bid_safe(client, task_id: str, agent_id: str, price: float = 80.0) -> dict:
    """Submit a bid, return response JSON regardless of status code."""
    resp = await client.post(f"/api/tasks/{task_id}/bid", json={
        "agent_id": agent_id, "confidence": 0.9, "price": price,
    })
    return {"status_code": resp.status_code, **resp.json()}


async def _submit_result_safe(client, task_id: str, agent_id: str) -> dict:
    resp = await client.post(f"/api/tasks/{task_id}/result", json={
        "agent_id": agent_id, "content": f"result from {agent_id}",
    })
    return {"status_code": resp.status_code, "body": resp.json()}


async def _select_result_safe(client, task_id: str, agent_id: str, initiator: str) -> dict:
    resp = await client.post(f"/api/tasks/{task_id}/select", json={
        "initiator_id": initiator, "agent_id": agent_id,
    })
    return {"status_code": resp.status_code, "body": resp.json()}


# ═════════════════════════════════════════════════════════════════════
# Test: Concurrent bids on same task
# ═════════════════════════════════════════════════════════════════════

class TestConcurrentBids:
    """Multiple agents bidding on the same task simultaneously (#29, #113)."""

    @pytest.mark.asyncio
    async def test_concurrent_bids_respect_slot_limit(self, client):
        """N concurrent bids on a max_concurrent=2 task: exactly 2 get EXECUTING."""
        await create_task(client, task_id="cb1", budget=500.0, max_concurrent_bidders=2)

        # 5 agents bid concurrently
        results = await asyncio.gather(*[
            _bid_safe(client, "cb1", f"a{i+1}", price=80.0)
            for i in range(5)
        ], return_exceptions=True)

        # Count how many got executing vs waiting vs rejected
        statuses = [r["status"] for r in results if isinstance(r, dict) and "status" in r]
        executing = statuses.count("executing")
        waiting = statuses.count("waiting")

        assert executing == 2, f"Expected 2 executing, got {executing}: {statuses}"
        assert waiting >= 1, f"Expected at least 1 waiting, got {waiting}: {statuses}"
        assert executing + waiting <= 5

    @pytest.mark.asyncio
    async def test_concurrent_bids_no_duplicates(self, client):
        """Same agent cannot double-bid even under concurrent requests."""
        await create_task(client, task_id="cb2", budget=500.0, max_concurrent_bidders=5)

        # Same agent bids 3 times concurrently
        results = await asyncio.gather(*[
            _bid_safe(client, "cb2", "a1", price=80.0)
            for _ in range(3)
        ], return_exceptions=True)

        successes = [r for r in results if isinstance(r, dict) and r.get("status_code") == 200]
        failures = [r for r in results if isinstance(r, dict) and r.get("status_code") == 400]

        assert len(successes) == 1, f"Expected exactly 1 success: {results}"
        assert len(failures) == 2, f"Expected 2 failures: {results}"

    @pytest.mark.asyncio
    async def test_10_agents_bid_simultaneously(self, client, funded_network):
        """Stress: 10 agents bid on the same task (max_concurrent=3)."""
        net = funded_network
        # Register 10 agents with reputation
        for i in range(10):
            aid = f"stress-a{i}"
            net.reputation._scores[aid] = 0.8
            await net.dht.announce("coding", aid)

        await create_task(client, task_id="stress-bid", budget=2000.0, max_concurrent_bidders=3)

        results = await asyncio.gather(*[
            _bid_safe(client, "stress-bid", f"stress-a{i}", price=80.0)
            for i in range(10)
        ])

        statuses = [r.get("status", "error") for r in results]
        executing = statuses.count("executing")
        waiting = statuses.count("waiting")

        assert executing == 3, f"Expected 3 executing, got {executing}: {statuses}"
        assert waiting == 7, f"Expected 7 waiting, got {waiting}: {statuses}"


# ═════════════════════════════════════════════════════════════════════
# Test: Concurrent result submission
# ═════════════════════════════════════════════════════════════════════

class TestConcurrentResults:
    """Multiple agents submitting results concurrently (#22)."""

    @pytest.mark.asyncio
    async def test_concurrent_result_submissions(self, client):
        """3 agents submit results concurrently — all should succeed."""
        await create_task(client, task_id="cr1", budget=500.0, max_concurrent_bidders=3)
        for i in range(3):
            await bid(client, task_id="cr1", agent_id=f"a{i+1}", price=80.0)

        results = await asyncio.gather(*[
            _submit_result_safe(client, "cr1", f"a{i+1}")
            for i in range(3)
        ])

        successes = [r for r in results if r["status_code"] == 200]
        assert len(successes) == 3, f"Expected 3 successes: {results}"

        # Auto-collect should have fired (3 results >= 3 max_concurrent)
        task = await _get_task(client, "cr1")
        assert task["status"] in ("awaiting_retrieval", "completed")

    @pytest.mark.asyncio
    async def test_duplicate_result_under_concurrency(self, client):
        """Same agent submitting result concurrently → only 1 succeeds (#33)."""
        await create_task(client, task_id="cr2", budget=200.0)
        await bid(client, task_id="cr2", agent_id="a1", price=80.0)

        results = await asyncio.gather(*[
            _submit_result_safe(client, "cr2", "a1")
            for _ in range(3)
        ])

        successes = [r for r in results if r["status_code"] == 200]
        failures = [r for r in results if r["status_code"] == 400]

        assert len(successes) == 1, f"Expected 1 success: {results}"
        assert len(failures) == 2, f"Expected 2 failures: {results}"


# ═════════════════════════════════════════════════════════════════════
# Test: Concurrent select_result (#21, #52)
# ═════════════════════════════════════════════════════════════════════

class TestConcurrentSelect:
    """Two concurrent select_result on same task → only 1 succeeds."""

    @pytest.mark.asyncio
    async def test_concurrent_select_only_one_wins(self, client):
        """Two concurrent selects → exactly 1 success, 1 failure (#21/#52)."""
        await create_task(client, task_id="cs1", budget=300.0, max_concurrent_bidders=2)
        await bid(client, task_id="cs1", agent_id="a1", price=80.0)
        await bid(client, task_id="cs1", agent_id="a2", price=70.0)
        await submit_result(client, task_id="cs1", agent_id="a1")
        await submit_result(client, task_id="cs1", agent_id="a2")

        await client.post("/api/tasks/cs1/close", json={"initiator_id": "user1"})

        # Two concurrent selects for different agents
        results = await asyncio.gather(
            _select_result_safe(client, "cs1", "a1", "user1"),
            _select_result_safe(client, "cs1", "a2", "user1"),
        )

        codes = [r["status_code"] for r in results]
        assert codes.count(200) == 1, f"Expected exactly 1 success: {codes}"
        assert codes.count(400) == 1, f"Expected exactly 1 failure: {codes}"

    @pytest.mark.asyncio
    async def test_no_double_payment(self, client):
        """Concurrent selects must not cause double payment (#21)."""
        bal_before = await _get_balance(client, "user1")

        await create_task(client, task_id="dp1", budget=200.0)
        await bid(client, task_id="dp1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dp1", agent_id="a1")
        await client.post("/api/tasks/dp1/close", json={"initiator_id": "user1"})

        # 3 concurrent selects for the same agent
        results = await asyncio.gather(*[
            _select_result_safe(client, "dp1", "a1", "user1")
            for _ in range(3)
        ])

        successes = [r for r in results if r["status_code"] == 200]
        assert len(successes) == 1, f"Expected exactly 1 success: {results}"

        # Executor should only be credited once
        a1_bal = await _get_balance(client, "a1")
        assert a1_bal["available"] == 80.0, f"Expected 80.0, got {a1_bal['available']}"


# ═════════════════════════════════════════════════════════════════════
# Test: Concurrent subtask creation
# ═════════════════════════════════════════════════════════════════════

class TestConcurrentSubtasks:
    """Multiple subtasks created concurrently from the same parent."""

    @pytest.mark.asyncio
    async def test_concurrent_subtasks_budget_consistent(self, client):
        """3 concurrent subtask creations (100 each) from a 250 budget parent.
        At most 2 should succeed; parent remaining_budget must be correct."""
        await create_task(client, task_id="csub1", budget=250.0)
        await bid(client, task_id="csub1", agent_id="a1", price=100.0)

        async def _create_sub(i: int) -> dict:
            resp = await client.post("/api/tasks/csub1/subtask", json={
                "initiator_id": "a1", "content": {"sub": i},
                "domains": ["coding"], "budget": 100.0,
            })
            return {"status_code": resp.status_code, "body": resp.json()}

        results = await asyncio.gather(*[_create_sub(i) for i in range(3)])
        successes = [r for r in results if r["status_code"] == 201]
        failures = [r for r in results if r["status_code"] == 400]

        assert len(successes) == 2, f"Expected 2 successes from 250 budget: {results}"
        assert len(failures) == 1, f"Expected 1 failure: {results}"

        parent = await _get_task(client, "csub1")
        assert parent["remaining_budget"] == 50.0, (
            f"Expected 50.0 remaining, got {parent['remaining_budget']}"
        )


# ═════════════════════════════════════════════════════════════════════
# Test: Bid + Close race
# ═════════════════════════════════════════════════════════════════════

class TestBidCloseRace:
    """Bid arriving concurrently with task close."""

    @pytest.mark.asyncio
    async def test_bid_during_close(self, client):
        """Concurrent bid + close: both may succeed or close wins, but no crash."""
        await create_task(client, task_id="bcr1", budget=200.0)

        bid_result, close_result = await asyncio.gather(
            _bid_safe(client, "bcr1", "a1", price=80.0),
            client.post("/api/tasks/bcr1/close", json={"initiator_id": "user1"}),
        )

        # Both should return without server error (200 or 400, not 500)
        assert bid_result.get("status_code", 200) in (200, 400), f"Bid crashed: {bid_result}"
        assert close_result.status_code in (200, 400), f"Close crashed: {close_result.text}"


# ═════════════════════════════════════════════════════════════════════
# Test: Mixed workload — N agents × M tasks (#114)
# ═════════════════════════════════════════════════════════════════════

class TestMixedWorkloadStress:
    """Simulate realistic multi-agent concurrent workload."""

    @pytest.mark.asyncio
    async def test_5_tasks_10_agents_full_lifecycle(self, client, funded_network):
        """Stress: 5 tasks created, 10 agents bid on all of them concurrently.
        Then submit results, close, and select. Verify final state consistency."""
        net = funded_network

        # Setup 10 agents
        agents = [f"mix-a{i}" for i in range(10)]
        for aid in agents:
            net.reputation._scores[aid] = 0.8
            await net.dht.announce("coding", aid)

        # Fund multiple initiators
        for i in range(5):
            net.escrow.get_or_create_account(f"mix-init{i}", 5000.0)

        # Phase 1: Create 5 tasks concurrently
        create_results = await asyncio.gather(*[
            create_task(client, task_id=f"mix-t{i}", initiator_id=f"mix-init{i}",
                        budget=500.0, max_concurrent_bidders=3)
            for i in range(5)
        ])
        assert all(r["status"] == "unclaimed" for r in create_results)

        # Phase 2: Each of 10 agents bids on all 5 tasks concurrently (50 bids total)
        bid_tasks = []
        for agent in agents:
            for i in range(5):
                bid_tasks.append(_bid_safe(client, f"mix-t{i}", agent, price=80.0))

        bid_results = await asyncio.gather(*bid_tasks)

        # No 500 errors
        for r in bid_results:
            assert r.get("status_code", 200) != 500, f"Server error: {r}"

        # Phase 3: First 3 agents on each task submit results
        for i in range(5):
            task = await _get_task(client, f"mix-t{i}")
            executing = [b["agent_id"] for b in task["bids"]
                        if b["status"] in ("executing", "accepted")]
            for agent_id in executing[:3]:
                await _submit_result_safe(client, f"mix-t{i}", agent_id)

        # Phase 4: Close and select on each task
        for i in range(5):
            task = await _get_task(client, f"mix-t{i}")
            if task["status"] in ("bidding", "awaiting_retrieval"):
                await client.post(f"/api/tasks/mix-t{i}/close",
                                  json={"initiator_id": f"mix-init{i}"})
            task = await _get_task(client, f"mix-t{i}")
            if task["results"]:
                first_result_agent = task["results"][0]["agent_id"]
                await _select_result_safe(client, f"mix-t{i}",
                                          first_result_agent, f"mix-init{i}")

        # Phase 5: Verify consistency
        for i in range(5):
            task = await _get_task(client, f"mix-t{i}")
            assert task["status"] in ("completed", "awaiting_retrieval", "no_one_able"), (
                f"Task mix-t{i} in unexpected status: {task['status']}"
            )

    @pytest.mark.asyncio
    async def test_rapid_create_bid_result_cycle(self, client, funded_network):
        """Rapid sequential: create-bid-result-close-select for 10 tasks."""
        net = funded_network
        net.reputation._scores["rapid-agent"] = 0.8
        await net.dht.announce("coding", "rapid-agent")

        async def full_lifecycle(i: int):
            tid = f"rapid-{i}"
            net.escrow.get_or_create_account(f"rapid-init-{i}", 500.0)
            await create_task(client, task_id=tid, initiator_id=f"rapid-init-{i}", budget=100.0)
            await bid(client, task_id=tid, agent_id="rapid-agent", price=50.0)
            await submit_result(client, task_id=tid, agent_id="rapid-agent")
            await close_task(client, task_id=tid, initiator_id=f"rapid-init-{i}")
            await select_result(client, task_id=tid, agent_id="rapid-agent",
                                initiator_id=f"rapid-init-{i}")
            task = await _get_task(client, tid)
            return task["status"]

        results = await asyncio.gather(*[full_lifecycle(i) for i in range(10)])
        assert all(s == "completed" for s in results), f"Not all completed: {results}"


# ═════════════════════════════════════════════════════════════════════
# Test: Escrow consistency under concurrent operations
# ═════════════════════════════════════════════════════════════════════

class TestEscrowConsistencyStress:
    """Verify escrow balances are consistent after concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_creates_drain_balance_correctly(self, client, funded_network):
        """Create 10 tasks of 1000 each from a 10000 balance → exactly 10 succeed."""
        net = funded_network
        # user1 has 10000

        results = await asyncio.gather(*[
            client.post("/api/tasks", json={
                "task_id": f"drain-{i}", "initiator_id": "user1",
                "content": {}, "domains": ["coding"], "budget": 1000.0,
            })
            for i in range(10)
        ])

        codes = [r.status_code for r in results]
        successes = codes.count(201)
        assert successes == 10, f"Expected 10 successes from 10000 balance: {codes}"

        # Balance should be 0 available, 10000 frozen
        bal = await _get_balance(client, "user1")
        assert bal["available"] == 0.0
        assert bal["frozen"] == 10000.0

    @pytest.mark.asyncio
    async def test_concurrent_creates_exceed_balance(self, client, funded_network):
        """11 tasks of 1000 each from 10000 balance → exactly 10 succeed, 1 fails."""
        net = funded_network
        # user1 has 10000

        results = await asyncio.gather(*[
            client.post("/api/tasks", json={
                "task_id": f"exceed-{i}", "initiator_id": "user1",
                "content": {}, "domains": ["coding"], "budget": 1000.0,
            })
            for i in range(11)
        ])

        codes = [r.status_code for r in results]
        successes = codes.count(201)
        failures = codes.count(402)  # BudgetError → 402

        assert successes == 10, f"Expected 10 successes: {codes}"
        assert failures == 1, f"Expected 1 budget failure: {codes}"


# ═════════════════════════════════════════════════════════════════════
# Test: Deadline expiry during active operations
# ═════════════════════════════════════════════════════════════════════

class TestDeadlineRaceConditions:
    """Deadline scan running while agents are actively bidding/submitting."""

    @pytest.mark.asyncio
    async def test_scan_during_bid_submission(self, client, funded_network):
        """Deadline scan fires while bids are being submitted — no crashes."""
        await create_task(client, task_id="drc1", budget=200.0,
                          deadline="2020-01-01T00:00:00Z")

        # Concurrently: bid + scan_deadlines
        bid_result, scan_result = await asyncio.gather(
            _bid_safe(client, "drc1", "a1", price=80.0),
            client.post("/api/admin/scan-deadlines"),
        )

        # Neither should crash
        assert bid_result.get("status_code", 200) in (200, 400)
        assert scan_result.status_code == 200

    @pytest.mark.asyncio
    async def test_result_during_deadline_scan(self, client):
        """Submit result concurrently with deadline scan — no inconsistency."""
        await create_task(client, task_id="drc2", budget=200.0,
                          deadline="2020-01-01T00:00:00Z")
        await bid(client, task_id="drc2", agent_id="a1", price=80.0)

        result_resp, scan_resp = await asyncio.gather(
            _submit_result_safe(client, "drc2", "a1"),
            client.post("/api/admin/scan-deadlines"),
        )

        # One of them may win the race — but no server error
        assert result_resp["status_code"] in (200, 400)
        assert scan_resp.status_code == 200
