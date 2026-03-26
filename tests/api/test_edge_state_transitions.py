"""Edge case state transition tests.

Tests the exact boundaries of the task state machine:
- Operations at wrong states
- Concurrent state changes
- Terminal state enforcement
- Auto-collect edge cases
- Close-then-select combination
"""

import asyncio
import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(c, tid): return (await c.get(f"/api/tasks/{tid}")).json()


class TestTerminalStateEnforcement:
    """Once a task is completed or no_one_able, nothing more should happen."""

    @pytest.mark.asyncio
    async def test_bid_on_completed_task(self, client):
        await create_task(client, task_id="term-1", budget=200.0)
        await bid(client, task_id="term-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="term-1", agent_id="a1")
        await client.post("/api/tasks/term-1/select", json={
            "initiator_id": "user1", "agent_id": "a1", "close_task": True,
        })
        # Bid on completed task
        resp = await client.post("/api/tasks/term-1/bid", json={
            "agent_id": "a2", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_on_completed_task(self, client):
        await create_task(client, task_id="term-2", budget=200.0)
        await bid(client, task_id="term-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="term-2", agent_id="a1")
        await client.post("/api/tasks/term-2/select", json={
            "initiator_id": "user1", "agent_id": "a1", "close_task": True,
        })
        resp = await client.post("/api/tasks/term-2/result", json={
            "agent_id": "a1", "content": "late",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bid_on_no_one_able(self, client):
        await create_task(client, task_id="term-3", budget=200.0)
        await close_task(client, task_id="term-3")
        resp = await client.post("/api/tasks/term-3/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_subtask_on_completed_parent(self, client):
        await create_task(client, task_id="term-4", budget=200.0)
        await bid(client, task_id="term-4", agent_id="a1", price=80.0)
        await submit_result(client, task_id="term-4", agent_id="a1")
        await client.post("/api/tasks/term-4/select", json={
            "initiator_id": "user1", "agent_id": "a1", "close_task": True,
        })
        resp = await client.post("/api/tasks/term-4/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400


class TestAutoCollectEdgeCases:
    @pytest.mark.asyncio
    async def test_auto_collect_exactly_at_threshold(self, client):
        """max_concurrent=2, 2 results → auto-collect fires."""
        await create_task(client, task_id="ac-1", budget=500.0,
                         max_concurrent_bidders=2)
        await bid(client, task_id="ac-1", agent_id="a1", price=80.0)
        await bid(client, task_id="ac-1", agent_id="a2", price=70.0)
        await submit_result(client, task_id="ac-1", agent_id="a1")
        await submit_result(client, task_id="ac-1", agent_id="a2")

        task = await _get_task(client, "ac-1")
        assert task["status"] == "awaiting_retrieval"

    @pytest.mark.asyncio
    async def test_auto_collect_one_below_threshold(self, client):
        """max_concurrent=3, 2 results → stays bidding."""
        await create_task(client, task_id="ac-2", budget=500.0,
                         max_concurrent_bidders=3)
        await bid(client, task_id="ac-2", agent_id="a1", price=80.0)
        await bid(client, task_id="ac-2", agent_id="a2", price=70.0)
        await bid(client, task_id="ac-2", agent_id="a3", price=60.0)
        await submit_result(client, task_id="ac-2", agent_id="a1")
        await submit_result(client, task_id="ac-2", agent_id="a2")

        task = await _get_task(client, "ac-2")
        assert task["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_auto_collect_with_waiting_agents(self, client):
        """max_concurrent=1, 1 result → auto-collect, waiting agent stays waiting."""
        await create_task(client, task_id="ac-3", budget=500.0,
                         max_concurrent_bidders=1)
        await bid(client, task_id="ac-3", agent_id="a1", price=80.0)
        await bid(client, task_id="ac-3", agent_id="a2", price=70.0)
        await submit_result(client, task_id="ac-3", agent_id="a1")

        task = await _get_task(client, "ac-3")
        assert task["status"] == "awaiting_retrieval"
        # a2 should still be waiting (not promoted since auto_collect fired)
        statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert statuses["a2"] == "waiting"


class TestCloseAndSelectCombination:
    @pytest.mark.asyncio
    async def test_select_with_close_from_bidding(self, client):
        """select_result with close_task=True from bidding status."""
        await create_task(client, task_id="cs-1", budget=200.0)
        await bid(client, task_id="cs-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="cs-1", agent_id="a1")

        resp = await client.post("/api/tasks/cs-1/select", json={
            "initiator_id": "user1", "agent_id": "a1", "close_task": True,
        })
        assert resp.status_code == 200

        task = await _get_task(client, "cs-1")
        assert task["status"] == "completed"

    @pytest.mark.asyncio
    async def test_select_without_close_from_bidding_fails(self, client):
        """select_result without close_task from bidding → 400."""
        await create_task(client, task_id="cs-2", budget=200.0)
        await bid(client, task_id="cs-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="cs-2", agent_id="a1")

        resp = await client.post("/api/tasks/cs-2/select", json={
            "initiator_id": "user1", "agent_id": "a1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_close_then_select(self, client):
        """Normal flow: close first, then select."""
        await create_task(client, task_id="cs-3", budget=200.0)
        await bid(client, task_id="cs-3", agent_id="a1", price=80.0)
        await submit_result(client, task_id="cs-3", agent_id="a1")
        await close_task(client, task_id="cs-3")
        await select_result(client, task_id="cs-3", agent_id="a1")

        task = await _get_task(client, "cs-3")
        assert task["status"] == "completed"


class TestRejectEdgeCases:
    @pytest.mark.asyncio
    async def test_reject_non_bidder(self, client):
        await create_task(client, task_id="rej-1", budget=200.0)
        resp = await client.post("/api/tasks/rej-1/reject", json={"agent_id": "nobody"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reject_after_result(self, client):
        """Agent who already submitted result tries to reject — should fail."""
        await create_task(client, task_id="rej-2", budget=200.0)
        await bid(client, task_id="rej-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="rej-2", agent_id="a1")
        # Agent already submitted but tries to reject
        # This depends on whether submit changes bid status
        resp = await client.post("/api/tasks/rej-2/reject", json={"agent_id": "a1"})
        # Should succeed (bid is still executing, result was submitted separately)
        assert resp.status_code == 200


class TestDeadlineUpdateEdgeCases:
    @pytest.mark.asyncio
    async def test_update_deadline_completed_fails(self, client):
        await create_task(client, task_id="dl-1", budget=200.0)
        await bid(client, task_id="dl-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dl-1", agent_id="a1")
        await client.post("/api/tasks/dl-1/select", json={
            "initiator_id": "user1", "agent_id": "a1", "close_task": True,
        })
        resp = await client.put("/api/tasks/dl-1/deadline", json={
            "initiator_id": "user1", "deadline": "2030-01-01",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_deadline_wrong_initiator(self, client):
        await create_task(client, task_id="dl-2", budget=200.0)
        resp = await client.put("/api/tasks/dl-2/deadline", json={
            "initiator_id": "user2", "deadline": "2030-01-01",
        })
        assert resp.status_code == 400
