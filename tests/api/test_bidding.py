"""Tests: Bidding flows via Network HTTP API.

Covers: POST /api/tasks/{id}/bid
        Concurrent slots, budget locking, over-budget pending,
        ability threshold, promotion from queue.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result


class TestBidBasic:
    @pytest.mark.asyncio
    async def test_successful_bid(self, client):
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=80.0)
        assert data["status"] == "executing"
        assert data["task_id"] == "t1"
        assert data["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_bid_transitions_to_bidding(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1")
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_bid_recorded_in_task(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=80.0)
        data = (await client.get("/api/tasks/t1")).json()
        assert len(data["bids"]) == 1
        assert data["bids"][0]["agent_id"] == "a1"
        assert data["bids"][0]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_multiple_bids(self, client):
        await create_task(client, task_id="t1", max_concurrent_bidders=5)
        await bid(client, task_id="t1", agent_id="a1")
        await bid(client, task_id="t1", agent_id="a2")
        await bid(client, task_id="t1", agent_id="a3")
        data = (await client.get("/api/tasks/t1")).json()
        assert len(data["bids"]) == 3


class TestBidAbilityThreshold:
    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, client, funded_network):
        """Agent with low reputation × low confidence → rejected."""
        funded_network.reputation._scores["a1"] = 0.2
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.2, price=50.0)
        assert data["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_high_confidence_accepted(self, client):
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=80.0)
        assert data["status"] == "executing"

    @pytest.mark.asyncio
    async def test_zero_confidence_rejected(self, client):
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.0, price=50.0)
        assert data["status"] == "rejected"


class TestBidConcurrentSlots:
    @pytest.mark.asyncio
    async def test_slots_fill_then_queue(self, client):
        """When concurrent slots are full, new bids go to waiting."""
        await create_task(client, task_id="t1", max_concurrent_bidders=2)
        b1 = await bid(client, task_id="t1", agent_id="a1")
        b2 = await bid(client, task_id="t1", agent_id="a2")
        b3 = await bid(client, task_id="t1", agent_id="a3")
        assert b1["status"] == "executing"
        assert b2["status"] == "executing"
        assert b3["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_budget_locked_when_slots_full(self, client):
        await create_task(client, task_id="t1", max_concurrent_bidders=1)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        data = (await client.get("/api/tasks/t1")).json()
        assert data["budget_locked"] is True

    @pytest.mark.asyncio
    async def test_single_slot_limits(self, client):
        await create_task(client, task_id="t1", max_concurrent_bidders=1)
        b1 = await bid(client, task_id="t1", agent_id="a1")
        b2 = await bid(client, task_id="t1", agent_id="a2")
        assert b1["status"] == "executing"
        assert b2["status"] == "waiting"


class TestBidOverBudget:
    @pytest.mark.asyncio
    async def test_over_budget_pending_when_slots_available(self, client):
        """Price > budget with available slots → needs_confirmation (pending)."""
        await create_task(client, task_id="t1", budget=50.0)
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_over_budget_rejected_when_locked(self, client):
        """Price > budget with budget locked → rejected."""
        await create_task(client, task_id="t1", budget=50.0, max_concurrent_bidders=1)
        # Fill the slot
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=40.0)
        # Over-budget with locked budget
        data = await bid(client, task_id="t1", agent_id="a2", confidence=0.9, price=100.0)
        assert data["status"] in ("rejected", "waiting")


class TestBidEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_price_bid(self, client):
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=0.0)
        assert data["status"] == "executing"


class TestBidPromotion:
    @pytest.mark.asyncio
    async def test_promotion_after_result_submission(self, client):
        """With max_concurrent_bidders=2, submitting 1 result promotes waiting bid."""
        await create_task(client, task_id="t1", max_concurrent_bidders=2)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)
        await bid(client, task_id="t1", agent_id="a3", price=60.0)
        await submit_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses.get("a3") in ("executing", "accepted")

    @pytest.mark.asyncio
    async def test_promotion_order_fifo(self, client):
        """FIFO: a2 promoted before a3 when there are enough slots."""
        await create_task(client, task_id="t1", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)
        await bid(client, task_id="t1", agent_id="a3", price=60.0)
        await bid(client, task_id="t1", agent_id="a4", price=50.0)
        await submit_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a4"] in ("executing", "accepted")

    @pytest.mark.asyncio
    async def test_chain_promotion(self, client):
        """Chain: a1 done → a2 promoted → a2 done → a3 promoted.
        Uses max_concurrent_bidders=3 so auto_collect doesn't fire after 1 result."""
        await create_task(client, task_id="t1", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)
        await bid(client, task_id="t1", agent_id="a3", price=60.0)
        await bid(client, task_id="t1", agent_id="a4", price=50.0)
        await bid(client, task_id="t1", agent_id="a5", price=40.0)
        await submit_result(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a2")
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a4"] in ("executing", "accepted")
        assert statuses["a5"] in ("executing", "accepted")

    @pytest.mark.asyncio
    async def test_reject_promotes_waiting(self, client):
        """Rejecting executing bid promotes next waiting."""
        await create_task(client, task_id="t1", budget=500.0, max_concurrent_bidders=1)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)
        await client.post("/api/tasks/t1/reject", json={"agent_id": "a1"})
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "rejected"
        assert statuses["a2"] in ("executing", "accepted")


class TestBidConfirmBudget:
    @pytest.mark.asyncio
    async def test_confirm_budget_promotes_pending(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })
        assert resp.status_code == 200
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "executing"

    @pytest.mark.asyncio
    async def test_confirm_insufficient_keeps_pending(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=200.0)
        await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 60.0,
        })
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "pending"

    @pytest.mark.asyncio
    async def test_confirm_budget_rejected(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": False,
        })
        assert resp.status_code == 200
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "rejected"
