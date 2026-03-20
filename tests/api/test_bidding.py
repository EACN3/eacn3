"""Tests: Bidding flows via Network HTTP API.

Covers: POST /api/tasks/{id}/bid
        Concurrent slots, budget locking, over-budget pending,
        ability threshold, promotion from queue.
"""

import pytest
from tests.api.conftest import create_task, bid


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
    async def test_bid_on_nonexistent_task(self, client):
        resp = await client.post("/api/tasks/nonexistent/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bid_on_closed_task(self, client):
        await create_task(client, task_id="t1")
        await client.post("/api/tasks/t1/close", json={"initiator_id": "user1"})
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_zero_price_bid(self, client):
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=0.0)
        assert data["status"] == "executing"

    @pytest.mark.asyncio
    async def test_validation_confidence_out_of_range(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 1.5, "price": 80.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_validation_negative_price(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": -10.0,
        })
        assert resp.status_code == 422


class TestBidPromotion:
    @pytest.mark.asyncio
    async def test_promotion_after_result_submission(self, client):
        """Waiting bid gets promoted when executing slot frees up."""
        await create_task(client, task_id="t1", max_concurrent_bidders=1)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)

        # a1 submits result → frees slot → a2 promoted
        await client.post("/api/tasks/t1/result", json={
            "agent_id": "a1", "content": "done",
        })
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        # a2 should have been promoted from waiting
        assert statuses.get("a2") in ("executing", "accepted")
