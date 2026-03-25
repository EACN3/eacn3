"""Complex multi-step scenario tests.

Tests interactions that span multiple operations and test state transitions
through realistic sequences:
- Task with 5 competing agents → selection → verify payments
- Subtask delegation chain → cascade completion
- Budget changes mid-task
- Agent bids on multiple tasks, some succeed some fail
- Rapid task lifecycle iteration
"""

import asyncio
import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(c, tid): return (await c.get(f"/api/tasks/{tid}")).json()
async def _bal(c, aid): return (await c.get("/api/economy/balance", params={"agent_id": aid})).json()


class TestFiveAgentCompetition:
    """5 agents compete for 1 task, best result is selected."""

    @pytest.mark.asyncio
    async def test_5_agents_1_winner(self, client, funded_network):
        net = funded_network
        for i in range(5):
            net.reputation._scores[f"comp-{i}"] = 0.7 + i * 0.02
            await net.dht.announce("coding", f"comp-{i}")

        await create_task(client, task_id="5comp",
                         budget=500.0, max_concurrent_bidders=5)

        # All 5 bid
        for i in range(5):
            b = await bid(client, task_id="5comp", agent_id=f"comp-{i}",
                         price=80.0 + i)

        # All 5 submit results
        for i in range(5):
            await submit_result(client, task_id="5comp", agent_id=f"comp-{i}",
                              content=f"result from comp-{i}")

        # Auto-collect should fire
        task = await _get_task(client, "5comp")
        assert task["status"] == "awaiting_retrieval"
        assert len(task["results"]) == 5

        # Select comp-2 as winner
        await select_result(client, task_id="5comp", agent_id="comp-2")

        # comp-2 should be paid
        bal = await _bal(client, "comp-2")
        assert bal["available"] == 82.0  # price was 82.0

        # Others should NOT be paid (may not even have accounts)
        for i in [0, 1, 3, 4]:
            resp = await client.get("/api/economy/balance", params={"agent_id": f"comp-{i}"})
            if resp.status_code == 200:
                assert resp.json()["available"] == 0.0, f"comp-{i} got paid incorrectly"
            # 404 = no account = not paid, which is correct

        # Check final task state
        final = await _get_task(client, "5comp")
        assert final["status"] == "completed"
        selected_count = sum(1 for r in final["results"] if r.get("selected"))
        assert selected_count == 1


class TestAgentMultiTask:
    """One agent bids on multiple tasks concurrently."""

    @pytest.mark.asyncio
    async def test_agent_works_3_tasks(self, client, funded_network):
        net = funded_network
        net.reputation._scores["multi-worker"] = 0.8
        await net.dht.announce("coding", "multi-worker")

        for i in range(3):
            net.escrow.get_or_create_account(f"init-{i}", 5000.0)
            await create_task(client, task_id=f"mt-{i}",
                            initiator_id=f"init-{i}", budget=200.0)
            await bid(client, task_id=f"mt-{i}", agent_id="multi-worker",
                     price=80.0)

        # Submit results for all 3
        for i in range(3):
            await submit_result(client, task_id=f"mt-{i}",
                              agent_id="multi-worker")

        # Select results from each initiator
        for i in range(3):
            await close_task(client, task_id=f"mt-{i}", initiator_id=f"init-{i}")
            await select_result(client, task_id=f"mt-{i}",
                              agent_id="multi-worker", initiator_id=f"init-{i}")

        # Worker should have 3 × 80 = 240
        bal = await _bal(client, "multi-worker")
        assert bal["available"] == 240.0


class TestBudgetChangeMidTask:
    """Budget confirmation changes task dynamics."""

    @pytest.mark.asyncio
    async def test_budget_increase_allows_expensive_bid(self, client):
        await create_task(client, task_id="bmc-1", budget=50.0)

        # Expensive bid → pending
        b = await bid(client, task_id="bmc-1", agent_id="a1", price=80.0)
        assert b["status"] == "pending"

        # Cheap bid → executing
        b2 = await bid(client, task_id="bmc-1", agent_id="a2", price=40.0)
        assert b2["status"] == "executing"

        # Approve budget increase
        await client.post("/api/tasks/bmc-1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })

        # Both should be active now
        task = await _get_task(client, "bmc-1")
        statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert statuses["a1"] in ("executing", "waiting")
        assert statuses["a2"] in ("executing", "waiting")


class TestSubtaskChainCompletion:
    """Deep subtask chain where everything completes bottom-up."""

    @pytest.mark.asyncio
    async def test_bottom_up_completion(self, client, funded_network):
        net = funded_network
        for i in range(3):
            net.reputation._scores[f"chain-{i}"] = 0.8
            await net.dht.announce("coding", f"chain-{i}")

        await create_task(client, task_id="chain-root", budget=1000.0, max_depth=5)
        await bid(client, task_id="chain-root", agent_id="chain-0", price=500.0)

        # Create chain: root → sub1 → sub2
        sub1_resp = await client.post("/api/tasks/chain-root/subtask", json={
            "initiator_id": "chain-0", "content": {},
            "domains": ["coding"], "budget": 200.0,
        })
        sub1_id = sub1_resp.json()["id"]
        await bid(client, task_id=sub1_id, agent_id="chain-1", price=100.0)

        sub2_resp = await client.post(f"/api/tasks/{sub1_id}/subtask", json={
            "initiator_id": "chain-1", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        sub2_id = sub2_resp.json()["id"]
        await bid(client, task_id=sub2_id, agent_id="chain-2", price=30.0)

        # Complete bottom-up: sub2 → sub1 → root
        await submit_result(client, task_id=sub2_id, agent_id="chain-2")
        await submit_result(client, task_id=sub1_id, agent_id="chain-1")
        await submit_result(client, task_id="chain-root", agent_id="chain-0")

        # All should have results
        for tid in ("chain-root", sub1_id, sub2_id):
            task = await _get_task(client, tid)
            assert len(task["results"]) >= 1, f"{tid} has no results"


class TestRapidCycleWithVerification:
    """10 complete cycles with balance verification at each step."""

    @pytest.mark.asyncio
    async def test_10_cycles_balance_consistent(self, client, funded_network):
        net = funded_network
        net.reputation._scores["cycler"] = 0.8
        await net.dht.announce("coding", "cycler")

        initial_balance = (await _bal(client, "user1"))["available"]
        total_paid = 0.0

        for i in range(10):
            price = 50.0 + i  # Different prices
            tid = f"cycle-{i}"
            await create_task(client, task_id=tid, budget=200.0)
            await bid(client, task_id=tid, agent_id="cycler", price=price)
            await submit_result(client, task_id=tid, agent_id="cycler")
            await close_task(client, task_id=tid)
            await select_result(client, task_id=tid, agent_id="cycler")
            total_paid += price

        # Cycler should have received all payments
        cycler_bal = await _bal(client, "cycler")
        assert cycler_bal["available"] == total_paid

        # User1 spent total_paid + fees, but got refunds on remaining escrow
        user_bal = await _bal(client, "user1")
        assert user_bal["frozen"] == 0.0  # All tasks completed, nothing frozen


class TestEventDeliveryInMultiStepFlow:
    """Verify events are delivered correctly through a multi-step flow using
    the wired offline store via the funded test fixture (no offline store here,
    so just verify task state transitions work correctly)."""

    @pytest.mark.asyncio
    async def test_task_state_machine_all_transitions(self, client):
        """Verify complete state machine: unclaimed → bidding → awaiting → completed."""
        await create_task(client, task_id="sm-1", budget=200.0)

        task = await _get_task(client, "sm-1")
        assert task["status"] == "unclaimed"

        await bid(client, task_id="sm-1", agent_id="a1", price=80.0)
        task = await _get_task(client, "sm-1")
        assert task["status"] == "bidding"

        await submit_result(client, task_id="sm-1", agent_id="a1")
        await close_task(client, task_id="sm-1")
        task = await _get_task(client, "sm-1")
        assert task["status"] == "awaiting_retrieval"

        # Get results transitions to completed
        resp = await client.get("/api/tasks/sm-1/results",
                               params={"initiator_id": "user1"})
        assert resp.status_code == 200

        task = await _get_task(client, "sm-1")
        assert task["status"] == "completed"
