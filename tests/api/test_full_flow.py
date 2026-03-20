"""Tests: End-to-end integration flows via Network HTTP API.

Covers complete task lifecycles and error handling across endpoints.
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


class TestFullTaskLifecycle:
    """Complete happy-path flows exercising multiple endpoints."""

    @pytest.mark.asyncio
    async def test_create_bid_result_close_select(self, client):
        """Full lifecycle: create → bid → result → close → select → completed."""
        # 1. Create task
        task = await create_task(client, task_id="flow1", budget=500.0)
        assert task["status"] == "unclaimed"

        # 2. Agent bids
        b = await bid(client, task_id="flow1", agent_id="a1", price=200.0)
        assert b["status"] == "executing"

        # 3. Verify bidding state
        data = (await client.get("/api/tasks/flow1")).json()
        assert data["status"] == "bidding"
        assert len(data["bids"]) == 1

        # 4. Submit result
        await submit_result(client, task_id="flow1", agent_id="a1")
        data = (await client.get("/api/tasks/flow1")).json()
        assert len(data["results"]) == 1

        # 5. Close task → awaiting_retrieval
        await close_task(client, task_id="flow1")

        # 6. Select result (triggers settlement)
        resp = await select_result(client, task_id="flow1", agent_id="a1")
        assert resp["ok"] is True

        # 7. Verify final state
        data = (await client.get("/api/tasks/flow1")).json()
        selected = [r for r in data["results"] if r.get("selected")]
        assert len(selected) == 1

        # 8. Verify logs
        logs = (await client.get("/api/admin/logs", params={"task_id": "flow1"})).json()
        fn_names = {l["fn_name"] for l in logs}
        assert {"create_task", "submit_bid", "submit_result", "select_result"} <= fn_names

    @pytest.mark.asyncio
    async def test_create_bid_result_close_collect(self, client):
        """Create → bid → result → close → collect → completed."""
        await create_task(client, task_id="flow2", budget=300.0)
        await bid(client, task_id="flow2", agent_id="a1")
        await submit_result(client, task_id="flow2", agent_id="a1")

        # Close → awaiting_retrieval
        data = await close_task(client, task_id="flow2")
        assert data["status"] == "awaiting_retrieval"

        # Collect results → completed
        resp = await client.get("/api/tasks/flow2/results", params={"initiator_id": "user1"})
        results = resp.json()
        assert len(results["results"]) == 1
        data = (await client.get("/api/tasks/flow2")).json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_no_candidates_close_refund(self, client):
        """Create task nobody bids on → close → no_one_able → budget refunded."""
        await create_task(client, task_id="flow3", budget=1000.0)
        await close_task(client, task_id="flow3")
        data = (await client.get("/api/tasks/flow3")).json()
        assert data["status"] == "no_one_able"

        # Budget refunded — can create another large task
        await create_task(client, task_id="flow4", budget=9000.0)


class TestMultiAgentFlow:
    @pytest.mark.asyncio
    async def test_competing_bids(self, client):
        """Multiple agents bid, one wins, others rejected."""
        await create_task(client, task_id="compete", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="compete", agent_id="a1", price=100.0)
        await bid(client, task_id="compete", agent_id="a2", price=120.0)
        await bid(client, task_id="compete", agent_id="a3", price=90.0)

        # All should be executing
        data = (await client.get("/api/tasks/compete")).json()
        assert len(data["bids"]) == 3

        # a1 submits result first
        await submit_result(client, task_id="compete", agent_id="a1", content="a1 solution")
        await submit_result(client, task_id="compete", agent_id="a2", content="a2 solution")

        # Close then select a1's result
        await close_task(client, task_id="compete")
        await select_result(client, task_id="compete", agent_id="a1")
        data = (await client.get("/api/tasks/compete")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "accepted"
        assert statuses["a2"] == "rejected"

class TestSubtaskFlow:
    @pytest.mark.asyncio
    async def test_parent_subtask_lifecycle(self, client):
        """Create parent → bid → subtask → bid on subtask → result → verify tree."""
        await create_task(client, task_id="parent", budget=1000.0)
        # Must bid on parent first (subtask creator must be a bidder)
        await bid(client, task_id="parent", agent_id="a1")
        sub = (await client.post("/api/tasks/parent/subtask", json={
            "initiator_id": "a1", "content": {"desc": "sub work"},
            "domains": ["coding"], "budget": 200.0,
        })).json()
        sub_id = sub["id"]
        assert sub["parent_id"] == "parent"

        # Bid and work on subtask
        await bid(client, task_id=sub_id, agent_id="a2", price=100.0)
        await submit_result(client, task_id=sub_id, agent_id="a2")

        # Verify parent knows about subtask
        parent = (await client.get("/api/tasks/parent")).json()
        assert sub_id in parent["child_ids"]
        assert parent["remaining_budget"] == 800.0


class TestDeadlineFlow:
    @pytest.mark.asyncio
    async def test_deadline_expiry_with_results(self, client):
        """Task expires with results → awaiting_retrieval → collect → completed."""
        await create_task(
            client, task_id="deadline", budget=200.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        await bid(client, task_id="deadline", agent_id="a1")
        await submit_result(client, task_id="deadline", agent_id="a1")

        # Scan deadlines
        await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        data = (await client.get("/api/tasks/deadline")).json()
        assert data["status"] == "awaiting_retrieval"

        # Collect
        resp = await client.get("/api/tasks/deadline/results", params={"initiator_id": "user1"})
        results = resp.json()
        assert len(results["results"]) == 1
        data = (await client.get("/api/tasks/deadline")).json()
        assert data["status"] == "completed"


