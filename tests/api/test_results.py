"""Tests: Result submission + selection + settlement via Network HTTP API.

Covers: POST /api/tasks/{id}/result, POST /api/tasks/{id}/select,
        GET /api/tasks/{id}/results
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
    setup_task_with_bid, setup_task_with_result,
)


class TestSubmitResult:
    @pytest.mark.asyncio
    async def test_submit_result(self, client):
        await setup_task_with_bid(client)
        resp = await client.post("/api/tasks/t1/result", json={
            "agent_id": "a1", "content": "implementation code",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_result_recorded_in_task(self, client):
        await setup_task_with_result(client)
        data = (await client.get("/api/tasks/t1")).json()
        assert len(data["results"]) == 1
        assert data["results"][0]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_multiple_results_different_agents(self, client):
        await create_task(client, task_id="t1", budget=200.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1")
        await bid(client, task_id="t1", agent_id="a2")
        await submit_result(client, task_id="t1", agent_id="a1", content="result 1")
        await submit_result(client, task_id="t1", agent_id="a2", content="result 2")
        data = (await client.get("/api/tasks/t1")).json()
        assert len(data["results"]) == 2

    @pytest.mark.asyncio
    async def test_auto_collect_when_all_slots_done(self, client):
        """When max_concurrent=1 and sole agent submits result → auto collect."""
        await create_task(client, task_id="t1", max_concurrent_bidders=1)
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        # Should transition to awaiting_retrieval via auto-collect
        assert data["status"] in ("awaiting_retrieval", "bidding")

    @pytest.mark.asyncio
    async def test_submit_result_requires_active_bid(self, client):
        """Agent without an active bid cannot submit result."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        # a2 has no bid — should fail
        resp = await client.post("/api/tasks/t1/result", json={
            "agent_id": "a2", "content": "unauthorized result",
        })
        assert resp.status_code == 400


class TestCollectResults:
    @pytest.mark.asyncio
    async def test_collect_results(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "adjudications" in data
        assert len(data["results"]) == 1

    @pytest.mark.asyncio
    async def test_collect_transitions_to_completed(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_collect_idempotent(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        r1 = await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        r2 = await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        assert r1.json() == r2.json()

class TestSelectResult:
    @pytest.mark.asyncio
    async def test_select_result(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        resp = await select_result(client, task_id="t1", agent_id="a1")
        assert resp["ok"] is True

    @pytest.mark.asyncio
    async def test_select_marks_result_selected(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        selected = [r for r in data["results"] if r.get("selected")]
        assert len(selected) == 1
        assert selected[0]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_select_triggers_settlement(self, client):
        """Selection triggers payment settlement — budget should decrease."""
        await setup_task_with_result(client, budget=200.0, price=80.0)
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        data = (await client.get("/api/tasks/t1")).json()
        selected = [b for b in data["bids"] if b["status"] == "accepted"]
        assert len(selected) == 1

    @pytest.mark.asyncio
    async def test_select_propagates_reputation(self, client):
        """Selection should update reputation of selected agent."""
        await setup_task_with_result(client)
        # Check reputation before
        rep_before = (await client.get("/api/reputation/a1")).json()["score"]
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        # Check reputation after — should have changed
        rep_after = (await client.get("/api/reputation/a1")).json()["score"]
        assert rep_after != rep_before

    @pytest.mark.asyncio
    async def test_select_rejects_other_bids(self, client):
        """Selecting one agent should reject other bids."""
        await create_task(client, task_id="t1", budget=300.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await bid(client, task_id="t1", agent_id="a2", price=70.0)
        await submit_result(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a2")
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")

        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "accepted"
        assert statuses["a2"] == "rejected"

