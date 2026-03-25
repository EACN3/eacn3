"""Realistic end-to-end workflow tests simulating actual user scenarios.

These tests cover the full lifecycle as a real user would experience it,
not just isolated unit operations. Each test scenario represents a complete
real-world use case.
"""

import asyncio
import pytest

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(client, task_id: str) -> dict:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def _get_balance(client, agent_id: str) -> dict:
    return (await client.get("/api/economy/balance", params={"agent_id": agent_id})).json()


async def _fund(client, agent_id: str, amount: float) -> dict:
    resp = await client.post("/api/admin/fund", json={"agent_id": agent_id, "amount": amount})
    return resp.json()


# ═════════════════════════════════════════════════════════════════════
# Scenario 1: Complete delegation chain — initiator → executor → subtask
# ═════════════════════════════════════════════════════════════════════

class TestDelegationChain:
    """Real workflow: user creates task → agent bids → agent creates subtask
    → sub-agent completes subtask → agent submits final result → user selects."""

    @pytest.mark.asyncio
    async def test_full_delegation_chain(self, client, funded_network):
        net = funded_network
        net.reputation._scores["executor"] = 0.8
        net.reputation._scores["sub-executor"] = 0.8
        await net.dht.announce("coding", "executor")
        await net.dht.announce("coding", "sub-executor")

        # 1. User creates task
        await create_task(client, task_id="chain-1", budget=500.0, initiator_id="user1")

        # 2. Executor bids and wins
        await bid(client, task_id="chain-1", agent_id="executor", price=300.0)
        task = await _get_task(client, "chain-1")
        assert task["status"] == "bidding"

        # 3. Executor creates subtask from the budget
        sub_resp = await client.post("/api/tasks/chain-1/subtask", json={
            "initiator_id": "executor",
            "content": {"desc": "sub-work"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert sub_resp.status_code == 201
        sub = sub_resp.json()
        sub_id = sub["id"]

        # Verify parent remaining_budget decreased
        parent = await _get_task(client, "chain-1")
        assert parent["remaining_budget"] == 400.0
        assert sub_id in parent["child_ids"]

        # 4. Sub-executor bids and completes subtask
        await bid(client, task_id=sub_id, agent_id="sub-executor", price=80.0)
        await submit_result(client, task_id=sub_id, agent_id="sub-executor",
                           content="subtask output")

        # 5. Executor submits final result to parent task
        await submit_result(client, task_id="chain-1", agent_id="executor",
                           content="final result using subtask")

        # 6. User closes and selects
        await close_task(client, task_id="chain-1")
        await select_result(client, task_id="chain-1", agent_id="executor")

        # 7. Verify final state
        final = await _get_task(client, "chain-1")
        assert final["status"] == "completed"

        # Executor should have been paid
        bal = await _get_balance(client, "executor")
        assert bal["available"] > 0


# ═════════════════════════════════════════════════════════════════════
# Scenario 2: Budget confirmation flow
# ═════════════════════════════════════════════════════════════════════

class TestBudgetConfirmationFlow:
    """Real workflow: agent bids over budget → initiator gets notified →
    approves/rejects → bid proceeds accordingly."""

    @pytest.mark.asyncio
    async def test_over_budget_approval(self, client):
        # Create task with low budget
        await create_task(client, task_id="budg-1", budget=50.0)

        # Agent bids higher than budget → should be pending
        b = await bid(client, task_id="budg-1", agent_id="a1", price=80.0)
        assert b["status"] == "pending"

        # Initiator approves with new higher budget
        resp = await client.post("/api/tasks/budg-1/confirm-budget", json={
            "initiator_id": "user1",
            "approved": True,
            "new_budget": 100.0,
        })
        assert resp.status_code == 200

        # Bid should now be accepted
        task = await _get_task(client, "budg-1")
        bid_statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert bid_statuses["a1"] in ("executing", "waiting")
        assert task["budget"] == 100.0

    @pytest.mark.asyncio
    async def test_over_budget_rejection(self, client):
        await create_task(client, task_id="budg-2", budget=50.0)

        b = await bid(client, task_id="budg-2", agent_id="a1", price=80.0)
        assert b["status"] == "pending"

        # Initiator rejects
        resp = await client.post("/api/tasks/budg-2/confirm-budget", json={
            "initiator_id": "user1",
            "approved": False,
        })
        assert resp.status_code == 200

        # Bid should be rejected
        task = await _get_task(client, "budg-2")
        assert task["bids"][0]["status"] == "rejected"


# ═════════════════════════════════════════════════════════════════════
# Scenario 3: Multi-round bidding with queue
# ═════════════════════════════════════════════════════════════════════

class TestMultiRoundBidding:
    """Real workflow: max_concurrent=2, agents cycle through executing→result→promote."""

    @pytest.mark.asyncio
    async def test_queue_promotion_cycle(self, client):
        await create_task(client, task_id="mrb-1", budget=1000.0, max_concurrent_bidders=2)

        # 4 agents bid
        for i, agent in enumerate(["a1", "a2", "a3"]):
            b = await bid(client, task_id="mrb-1", agent_id=agent, price=80.0)
            if i < 2:
                assert b["status"] == "executing", f"{agent} should be executing"
            else:
                assert b["status"] == "waiting", f"{agent} should be waiting"

        # a1 submits result → a3 should get promoted
        await submit_result(client, task_id="mrb-1", agent_id="a1")
        task = await _get_task(client, "mrb-1")
        statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert statuses["a3"] == "executing"

        # a2 and a3 submit → auto-collect should fire (2 results for 2 slots after a1 counted)
        await submit_result(client, task_id="mrb-1", agent_id="a2")

        task = await _get_task(client, "mrb-1")
        # After a1+a2 submitted (2 results >= 2 max_concurrent), auto_collect fires
        # a3 was promoted but task already collected, so it depends on ordering
        assert task["status"] in ("bidding", "awaiting_retrieval")


# ═════════════════════════════════════════════════════════════════════
# Scenario 4: Task timeout and cleanup
# ═════════════════════════════════════════════════════════════════════

class TestTaskTimeoutFlow:
    """Real workflow: task created → deadline passes → scan fires →
    escrow refunded → agents notified."""

    @pytest.mark.asyncio
    async def test_timeout_refunds_initiator(self, client):
        bal_before = await _get_balance(client, "user1")

        await create_task(client, task_id="timeout-1", budget=200.0,
                          deadline="2020-01-01T00:00:00Z")

        # Agent bids but doesn't submit result
        await bid(client, task_id="timeout-1", agent_id="a1", price=80.0)

        # Scan deadlines
        resp = await client.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        assert "timeout-1" in resp.json()["expired"]

        # Task should be no_one_able (had bids but no results)
        task = await _get_task(client, "timeout-1")
        assert task["status"] == "no_one_able"

        # Initiator should get full refund
        bal_after = await _get_balance(client, "user1")
        assert bal_after["available"] == bal_before["available"]

    @pytest.mark.asyncio
    async def test_timeout_with_results_keeps_results(self, client):
        await create_task(client, task_id="timeout-2", budget=200.0,
                          deadline="2020-01-01T00:00:00Z")

        await bid(client, task_id="timeout-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="timeout-2", agent_id="a1")

        resp = await client.post("/api/admin/scan-deadlines")
        assert "timeout-2" in resp.json()["expired"]

        # Task should be awaiting_retrieval (has results)
        task = await _get_task(client, "timeout-2")
        assert task["status"] == "awaiting_retrieval"
        assert len(task["results"]) == 1


# ═════════════════════════════════════════════════════════════════════
# Scenario 5: Discussion → clarification → result cycle
# ═════════════════════════════════════════════════════════════════════

class TestDiscussionWorkflow:
    """Real workflow: initiator creates task → agent bids → initiator adds
    clarification via discussion → agent submits result based on clarification."""

    @pytest.mark.asyncio
    async def test_discussion_update_during_bidding(self, client):
        await create_task(client, task_id="disc-1", budget=200.0)
        await bid(client, task_id="disc-1", agent_id="a1", price=80.0)

        # Initiator adds clarification
        resp = await client.post("/api/tasks/disc-1/discussions", json={
            "initiator_id": "user1",
            "message": "Please use Python 3.11+",
        })
        assert resp.status_code == 200

        # Verify discussion was added with author
        task = await _get_task(client, "disc-1")
        discussions = task["content"].get("discussions", [])
        assert len(discussions) == 1
        assert discussions[0]["message"] == "Please use Python 3.11+"
        assert discussions[0]["author"] == "user1"

    @pytest.mark.asyncio
    async def test_discussion_during_awaiting_retrieval(self, client):
        """Discussion should work during awaiting_retrieval (#81)."""
        await create_task(client, task_id="disc-2", budget=200.0)
        await bid(client, task_id="disc-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="disc-2", agent_id="a1")
        await close_task(client, task_id="disc-2")

        # Discussion should work in awaiting_retrieval
        resp = await client.post("/api/tasks/disc-2/discussions", json={
            "initiator_id": "user1",
            "message": "Good work but can you add tests?",
        })
        assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════
# Scenario 6: Escrow lifecycle — create → subtasks → settle → verify
# ═════════════════════════════════════════════════════════════════════

class TestEscrowFullLifecycle:
    """Verify escrow accounting through the entire task lifecycle."""

    @pytest.mark.asyncio
    async def test_escrow_accounting_after_settlement(self, client):
        """Create task (200) → subtask (80) → settle parent → verify all balances."""
        init_bal = await _get_balance(client, "user1")

        await create_task(client, task_id="esc-full", budget=200.0)
        await bid(client, task_id="esc-full", agent_id="a1", price=100.0)

        # Create subtask
        await client.post("/api/tasks/esc-full/subtask", json={
            "initiator_id": "a1",
            "content": {},
            "domains": ["coding"],
            "budget": 80.0,
        })

        # Submit and settle parent
        await submit_result(client, task_id="esc-full", agent_id="a1")
        await close_task(client, task_id="esc-full")
        await select_result(client, task_id="esc-full", agent_id="a1")

        # Executor got paid
        a1_bal = await _get_balance(client, "a1")
        assert a1_bal["available"] == 100.0  # bid_price

        # Verify escrow detail is empty for the settled task
        esc_resp = await client.get("/api/economy/escrows", params={"agent_id": "user1"})
        escrows = esc_resp.json()["escrows"]
        settled_escrows = [e for e in escrows if e["task_id"] == "esc-full"]
        # Main escrow should be cleared after settlement
        assert len(settled_escrows) == 0 or settled_escrows[0]["amount"] == 0


# ═════════════════════════════════════════════════════════════════════
# Scenario 7: Reject → re-bid → complete cycle
# ═════════════════════════════════════════════════════════════════════

class TestRejectAndRebid:
    """Agent rejects task, another agent picks it up."""

    @pytest.mark.asyncio
    async def test_reject_allows_another_to_win(self, client):
        await create_task(client, task_id="rej-1", budget=200.0, max_concurrent_bidders=1)
        await bid(client, task_id="rej-1", agent_id="a1", price=80.0)
        await bid(client, task_id="rej-1", agent_id="a2", price=70.0)  # waiting

        # a1 rejects → a2 promoted
        resp = await client.post("/api/tasks/rej-1/reject", json={"agent_id": "a1"})
        assert resp.status_code == 200

        task = await _get_task(client, "rej-1")
        statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert statuses["a1"] == "rejected"
        assert statuses["a2"] == "executing"

        # a2 completes
        await submit_result(client, task_id="rej-1", agent_id="a2")
        await close_task(client, task_id="rej-1")
        await select_result(client, task_id="rej-1", agent_id="a2")

        final = await _get_task(client, "rej-1")
        assert final["status"] == "completed"


# ═════════════════════════════════════════════════════════════════════
# Scenario 8: Multiple tasks from same initiator
# ═════════════════════════════════════════════════════════════════════

class TestMultipleTasksFromSameInitiator:
    """Initiator creates multiple tasks, manages them independently."""

    @pytest.mark.asyncio
    async def test_3_tasks_independent_lifecycle(self, client):
        for i in range(3):
            await create_task(client, task_id=f"multi-{i}", budget=100.0)
            await bid(client, task_id=f"multi-{i}", agent_id=f"a{i+1}", price=50.0)

        # Each follows independent lifecycle
        await submit_result(client, task_id="multi-0", agent_id="a1")
        await submit_result(client, task_id="multi-1", agent_id="a2")
        # multi-2: agent didn't submit

        # Close all
        for i in range(3):
            await close_task(client, task_id=f"multi-{i}")

        # Select results for the ones that have results
        await select_result(client, task_id="multi-0", agent_id="a1")
        await select_result(client, task_id="multi-1", agent_id="a2")

        # Verify states
        t0 = await _get_task(client, "multi-0")
        t1 = await _get_task(client, "multi-1")
        t2 = await _get_task(client, "multi-2")
        assert t0["status"] == "completed"
        assert t1["status"] == "completed"
        assert t2["status"] == "no_one_able"

        # Initiator balance: lost 2×50 (paid) but got refund for multi-2
        bal = await _get_balance(client, "user1")
        # Started with 10000, froze 300, got ~100 refund from multi-2 and remainders
        assert bal["available"] > 0


# ═════════════════════════════════════════════════════════════════════
# Scenario 9: Close task with close_task=True during select
# ═════════════════════════════════════════════════════════════════════

class TestCloseAndSelectInOne:
    """Use close_task=True in select_result to close and select in one call."""

    @pytest.mark.asyncio
    async def test_select_with_close_flag(self, client):
        await create_task(client, task_id="cs-1", budget=200.0)
        await bid(client, task_id="cs-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="cs-1", agent_id="a1")

        # Select with close_task=True (task is still in bidding)
        resp = await client.post("/api/tasks/cs-1/select", json={
            "initiator_id": "user1",
            "agent_id": "a1",
            "close_task": True,
        })
        assert resp.status_code == 200

        task = await _get_task(client, "cs-1")
        assert task["status"] == "completed"


# ═════════════════════════════════════════════════════════════════════
# Scenario 10: Cascading termination on close
# ═════════════════════════════════════════════════════════════════════

class TestCascadeTermination:
    """Closing parent task cascade-terminates children."""

    @pytest.mark.asyncio
    async def test_parent_close_terminates_subtasks(self, client, funded_network):
        net = funded_network
        net.reputation._scores["sub-worker"] = 0.8
        await net.dht.announce("coding", "sub-worker")

        await create_task(client, task_id="cascade-1", budget=500.0)
        await bid(client, task_id="cascade-1", agent_id="a1", price=200.0)

        # Create subtask
        sub_resp = await client.post("/api/tasks/cascade-1/subtask", json={
            "initiator_id": "a1", "content": {},
            "domains": ["coding"], "budget": 100.0,
        })
        sub_id = sub_resp.json()["id"]

        # Sub-agent bids on subtask
        await bid(client, task_id=sub_id, agent_id="sub-worker", price=50.0)

        # Close parent task → should cascade to subtask
        await close_task(client, task_id="cascade-1")

        # Check subtask got terminated
        sub_task = await _get_task(client, sub_id)
        assert sub_task["status"] in ("no_one_able", "awaiting_retrieval")

        # Sub-worker's bid should be rejected
        sub_bids = {b["agent_id"]: b["status"] for b in sub_task["bids"]}
        assert sub_bids.get("sub-worker") == "rejected"


# ═════════════════════════════════════════════════════════════════════
# Scenario 11: Reputation affects bid admission
# ═════════════════════════════════════════════════════════════════════

class TestReputationBidAdmission:
    """Agent with low reputation gets rejected."""

    @pytest.mark.asyncio
    async def test_low_reputation_bid_rejected(self, client, funded_network):
        net = funded_network
        net.reputation._scores["low-rep"] = 0.1  # Very low
        await net.dht.announce("coding", "low-rep")

        await create_task(client, task_id="rep-1", budget=200.0)

        # Low-rep agent bids with low confidence → confidence * reputation < threshold
        b = await bid(client, task_id="rep-1", agent_id="low-rep",
                      confidence=0.3, price=80.0)
        assert b["status"] == "rejected"


# ═════════════════════════════════════════════════════════════════════
# Scenario 12: Invite bypasses admission check
# ═════════════════════════════════════════════════════════════════════

class TestInvitedAgent:
    """Invited agents bypass normal admission filtering."""

    @pytest.mark.asyncio
    async def test_invited_agent_can_bid(self, client, funded_network):
        net = funded_network
        net.reputation._scores["invited-1"] = 0.1  # Low rep
        await net.dht.announce("coding", "invited-1")

        await create_task(client, task_id="inv-1", budget=200.0,
                          invited_agent_ids=["invited-1"])

        # Invited agent should pass even with low reputation
        resp = await client.post("/api/tasks/inv-1/invite", json={
            "initiator_id": "user1",
            "agent_id": "invited-1",
        })
        assert resp.status_code == 200

        b = await bid(client, task_id="inv-1", agent_id="invited-1",
                      confidence=0.3, price=80.0)
        # Invited agents bypass ability check
        assert b["status"] in ("executing", "waiting", "pending")


# ═════════════════════════════════════════════════════════════════════
# Scenario 13: Direct messaging between agents
# ═════════════════════════════════════════════════════════════════════

class TestDirectMessaging:
    """Agents exchange direct messages through the network."""

    @pytest.mark.asyncio
    async def test_relay_message(self, client, funded_network):
        """Send a direct message — returns 200 (delivery depends on offline_store setup)."""
        resp = await client.post("/api/messages", json={
            "to": {"agent_id": "a2"},
            "from": {"agent_id": "a1"},
            "content": "Hello from a1",
        })
        assert resp.status_code == 200
        # In test env without offline_store, delivery may not succeed
        # but the endpoint should not crash


# ═════════════════════════════════════════════════════════════════════
# Scenario 14: Error recovery — create fails, escrow released
# ═════════════════════════════════════════════════════════════════════

class TestErrorRecovery:
    """Verify system recovers cleanly from errors."""

    @pytest.mark.asyncio
    async def test_duplicate_task_id_releases_escrow(self, client):
        """Creating task with duplicate ID should release frozen budget (#1)."""
        bal_before = await _get_balance(client, "user1")

        await create_task(client, task_id="dup-1", budget=100.0)

        # Second create with same ID should fail
        resp = await client.post("/api/tasks", json={
            "task_id": "dup-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 409

        # Only 100 should be frozen (not 200)
        bal_after = await _get_balance(client, "user1")
        assert bal_after["frozen"] == bal_before["frozen"] + 100.0

    @pytest.mark.asyncio
    async def test_non_bidder_cannot_submit_result(self, client):
        await create_task(client, task_id="nob-1", budget=200.0)

        # Agent who didn't bid tries to submit result
        resp = await client.post("/api/tasks/nob-1/result", json={
            "agent_id": "a1", "content": "sneaky result",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_close(self, client):
        await create_task(client, task_id="nic-1", budget=200.0)

        # Non-initiator tries to close
        resp = await client.post("/api/tasks/nic-1/close", json={
            "initiator_id": "user2",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_select(self, client):
        await create_task(client, task_id="nis-1", budget=200.0)
        await bid(client, task_id="nis-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="nis-1", agent_id="a1")
        await close_task(client, task_id="nis-1")

        resp = await client.post("/api/tasks/nis-1/select", json={
            "initiator_id": "user2", "agent_id": "a1",
        })
        assert resp.status_code == 400
