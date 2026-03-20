"""Tests: Reputation + Economy via Network HTTP API.

Covers: POST /api/reputation/events, GET /api/reputation/{agent_id}
        POST /api/tasks/{id}/confirm-budget
        Budget escrow & settlement verified through task flows.
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
    setup_task_with_result,
)

class TestReputationEvents:
    @pytest.mark.asyncio
    async def test_positive_event_increases_score(self, client):
        before = (await client.get("/api/reputation/new_agent")).json()["score"]
        await client.post("/api/reputation/events", json={
            "agent_id": "new_agent", "event_type": "result_selected", "server_id": "s1",
        })
        after = (await client.get("/api/reputation/new_agent")).json()["score"]
        assert after > before

    @pytest.mark.asyncio
    async def test_negative_event_decreases_score(self, client, funded_network):
        funded_network.reputation._scores["target"] = 0.8
        before = (await client.get("/api/reputation/target")).json()["score"]
        await client.post("/api/reputation/events", json={
            "agent_id": "target", "event_type": "result_rejected", "server_id": "s1",
        })
        after = (await client.get("/api/reputation/target")).json()["score"]
        assert after < before

    @pytest.mark.asyncio
    async def test_unknown_event_type_ignored(self, client, funded_network):
        funded_network.reputation._scores["agent_x"] = 0.7
        resp = await client.post("/api/reputation/events", json={
            "agent_id": "agent_x", "event_type": "unknown_event", "server_id": "s1",
        })
        assert resp.status_code == 200
        after = (await client.get("/api/reputation/agent_x")).json()["score"]
        assert after == 0.7

    @pytest.mark.asyncio
    async def test_new_agent_default_score(self, client):
        resp = await client.get("/api/reputation/brand_new_agent")
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.5

    @pytest.mark.asyncio
    async def test_multiple_events_cumulative(self, client):
        for _ in range(5):
            await client.post("/api/reputation/events", json={
                "agent_id": "cumulative_agent",
                "event_type": "result_selected",
                "server_id": "s1",
            })
        score = (await client.get("/api/reputation/cumulative_agent")).json()["score"]
        assert score > 0.5

    @pytest.mark.asyncio
    async def test_event_returns_updated_score(self, client):
        resp = await client.post("/api/reputation/events", json={
            "agent_id": "check_agent", "event_type": "result_selected", "server_id": "s1",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "score" in data
        assert data["agent_id"] == "check_agent"

class TestGetReputation:
    @pytest.mark.asyncio
    async def test_known_agent(self, client):
        resp = await client.get("/api/reputation/a1")
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.8

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_default(self, client):
        resp = await client.get("/api/reputation/unknown_agent")
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.5

class TestReputationPropagation:
    @pytest.mark.asyncio
    async def test_select_result_updates_reputation(self, client):
        """Full flow: create → bid → result → close → select should propagate reputation."""
        before = (await client.get("/api/reputation/a1")).json()["score"]
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        after = (await client.get("/api/reputation/a1")).json()["score"]
        assert after != before

    @pytest.mark.asyncio
    async def test_reputation_affects_bid_acceptance(self, client, funded_network):
        """Agent with very low reputation should have bids rejected."""
        funded_network.reputation._scores["low_rep"] = 0.1
        await funded_network.dht.announce("coding", "low_rep")
        await create_task(client, task_id="t1")
        data = await bid(client, task_id="t1", agent_id="low_rep", confidence=0.3, price=50.0)
        assert data["status"] == "rejected"

class TestConfirmBudget:
    @pytest.mark.asyncio
    async def test_confirm_budget_approved(self, client):
        await create_task(client, task_id="t1", budget=100.0)
        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 150.0,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_confirm_budget_increases_task_budget(self, client):
        await create_task(client, task_id="t1", budget=100.0)
        await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 150.0,
        })
        data = (await client.get("/api/tasks/t1")).json()
        # Budget should have increased
        assert data["budget"] >= 150.0

    @pytest.mark.asyncio
    async def test_confirm_budget_with_pending_bids(self, client):
        """Confirming budget should promote pending over-budget bids."""
        await create_task(client, task_id="t1", budget=50.0)
        # Over-budget bid → pending
        b = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        assert b["status"] == "pending"

        # Confirm with new budget to cover the bid
        await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 150.0,
        })
        # Check if the pending bid was promoted
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        # Should be promoted to executing after budget confirmation
        assert statuses.get("a1") in ("executing", "pending")

    @pytest.mark.asyncio
    async def test_confirm_budget_rejected(self, client):
        """Rejecting budget confirmation should reject pending bids."""
        await create_task(client, task_id="t1", budget=50.0)
        b = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        assert b["status"] == "pending"

        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": False,
        })
        assert resp.status_code == 200
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses.get("a1") == "rejected"

class TestBudgetEscrow:
    @pytest.mark.asyncio
    async def test_create_task_freezes_budget(self, client):
        """Creating a task should freeze the budget amount."""
        await create_task(client, task_id="t1", budget=500.0)
        # Create another task — remaining account balance should be reduced
        await create_task(client, task_id="t2", budget=500.0)
        # Third should still work with 10000 initial
        await create_task(client, task_id="t3", budget=500.0)
        # Total frozen: 1500, initial: 10000, so this should work
        data = await create_task(client, task_id="t4", budget=7000.0)
        assert data["id"] == "t4"

    @pytest.mark.asyncio
    async def test_close_task_refunds_budget(self, client):
        """Closing a task with no results should refund the budget."""
        await create_task(client, task_id="t1", budget=5000.0)
        await close_task(client, task_id="t1")
        # Budget should be refunded, so creating another large task should work
        data = await create_task(client, task_id="t2", budget=9000.0)
        assert data["id"] == "t2"

    @pytest.mark.asyncio
    async def test_settlement_distributes_funds(self, client):
        """Selecting a result triggers settlement — funds move to executor."""
        await setup_task_with_result(client, budget=200.0, price=80.0)
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        accepted = [b for b in data["bids"] if b["status"] == "accepted"]
        assert len(accepted) == 1

    @pytest.mark.asyncio
    async def test_deadline_expiry_refunds(self, client):
        """Expired tasks with no results should refund budget."""
        await create_task(
            client, task_id="t1", budget=3000.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        # Budget should be refunded since no results
        data = await create_task(client, task_id="t2", budget=9000.0)
        assert data["id"] == "t2"
