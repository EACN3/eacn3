"""Tests: Subtask delegation via Network HTTP API.

Covers: POST /api/tasks/{id}/subtask
        Depth limits, budget allocation, inheritance.
"""

import pytest
from tests.api.conftest import create_task, bid

class TestCreateSubtask:
    @pytest.mark.asyncio
    async def test_basic_subtask(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1",
            "content": {"desc": "sub work"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["parent_id"] == "t1"
        assert data["depth"] == 1
        assert data["budget"] == 50.0

    @pytest.mark.asyncio
    async def test_subtask_deducts_parent_remaining_budget(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 80.0,
        })
        parent = (await client.get("/api/tasks/t1")).json()
        assert parent["remaining_budget"] == 120.0

    @pytest.mark.asyncio
    async def test_subtask_shows_in_parent_child_ids(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 30.0,
        })).json()
        parent = (await client.get("/api/tasks/t1")).json()
        assert sub["id"] in parent["child_ids"]

    @pytest.mark.asyncio
    async def test_multiple_subtasks_budget_tracking(self, client):
        await create_task(client, task_id="t1", budget=300.0)
        await bid(client, task_id="t1", agent_id="a1")
        await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 80.0,
        })
        parent = (await client.get("/api/tasks/t1")).json()
        assert parent["remaining_budget"] == 120.0

    @pytest.mark.asyncio
    async def test_subtask_budget_exceeds_parent_fails(self, client):
        await create_task(client, task_id="t1", budget=100.0)
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        assert resp.status_code == 400

class TestSubtaskInheritance:
    @pytest.mark.asyncio
    async def test_inherits_type(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 50.0,
        })).json()
        assert sub["type"] == "normal"

    @pytest.mark.asyncio
    async def test_subtask_different_domain(self, client):
        await create_task(client, task_id="t1", budget=200.0, domains=["coding"])
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["design"], "budget": 50.0,
        })).json()
        assert sub["domains"] == ["design"]

class TestSubtaskDepthLimits:
    @pytest.mark.asyncio
    async def test_nested_subtask(self, client):
        await create_task(client, task_id="t1", budget=500.0, max_depth=5)
        await bid(client, task_id="t1", agent_id="a1")
        sub1 = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 200.0,
        })).json()
        await bid(client, task_id=sub1["id"], agent_id="a2", price=50.0)
        sub2 = (await client.post(f"/api/tasks/{sub1['id']}/subtask", json={
            "initiator_id": "a2", "content": {}, "domains": ["coding"], "budget": 50.0,
        })).json()
        assert sub2["depth"] == 2

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded_fails(self, client):
        # max_depth=2: allows depth 1 subtask, rejects depth 2 (#45 off-by-one fix)
        await create_task(client, task_id="t1", budget=500.0, max_depth=2)
        await bid(client, task_id="t1", agent_id="a1")
        sub1 = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 200.0,
        })).json()
        # depth=1, max_depth=2: allowed
        await bid(client, task_id=sub1["id"], agent_id="a2", price=50.0)
        # depth=2, max_depth=2: now rejected (>= check)
        resp = await client.post(f"/api/tasks/{sub1['id']}/subtask", json={
            "initiator_id": "a2", "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_zero_budget_subtask(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 0.0,
        })
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_exact_remaining_budget_subtask(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 200.0,
        })
        assert resp.status_code == 201
        parent = (await client.get("/api/tasks/t1")).json()
        assert parent["remaining_budget"] == 0.0

class TestSubtaskLifecycle:
    @pytest.mark.asyncio
    async def test_subtask_appears_in_task_list(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        all_tasks = (await client.get("/api/tasks")).json()
        assert len(all_tasks) == 2

    @pytest.mark.asyncio
    async def test_subtask_independent_bidding(self, client):
        """Subtask can receive bids independently of parent."""
        await create_task(client, task_id="t1", budget=300.0)
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })).json()
        b = await bid(client, task_id=sub["id"], agent_id="a2", confidence=0.9, price=50.0)
        assert b["status"] == "executing"
