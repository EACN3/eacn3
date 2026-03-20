"""Tests: 所有接口的错误路径.

验证: 400/403/404/409/422 各种错误场景.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task


class TestCreateTaskErrors:
    @pytest.mark.asyncio
    async def test_duplicate_409(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        resp = await client.post("/api/tasks", json={
            "task_id": "t1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_insufficient_funds_402(self, api):
        resp = await api.post("/api/tasks", json={
            "task_id": "t1", "initiator_id": "broke",
            "content": {}, "domains": ["coding"], "budget": 99999.0,
        })
        assert resp.status_code == 402

    @pytest.mark.asyncio
    async def test_empty_domains_422(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "t1", "initiator_id": "user1",
            "content": {}, "domains": [], "budget": 100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_budget_422(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "t1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": -1.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_task_id_422(self, client):
        resp = await client.post("/api/tasks", json={
            "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 422


class TestBidErrors:
    @pytest.mark.asyncio
    async def test_bid_on_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bid_on_closed_task_400(self, client):
        await create_task(client, task_id="t1")
        await close_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_confidence_over_1_422(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 1.5, "price": 80.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_confidence_422(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": -0.1, "price": 80.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_price_422(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": -10.0,
        })
        assert resp.status_code == 422


class TestResultErrors:
    @pytest.mark.asyncio
    async def test_result_on_unclaimed_400(self, client):
        await create_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/result", json={
            "agent_id": "a1", "content": "x",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_on_closed_400(self, client):
        await create_task(client, task_id="t1")
        await close_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/result", json={
            "agent_id": "a1", "content": "x",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_on_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/result", json={
            "agent_id": "a1", "content": "x",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_select_nonexistent_agent_400(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        await close_task(client, task_id="t1")
        resp = await client.post("/api/tasks/t1/select", json={
            "initiator_id": "user1", "agent_id": "ghost",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_collect_nonexistent_404(self, client):
        resp = await client.get("/api/tasks/ghost/results", params={"initiator_id": "user1"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_collect_wrong_initiator_403(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        await close_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1/results", params={"initiator_id": "hacker"})
        assert resp.status_code == 403


class TestRejectErrors:
    @pytest.mark.asyncio
    async def test_reject_nonexistent_task_400(self, client):
        resp = await client.post("/api/tasks/ghost/reject", json={"agent_id": "a1"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reject_nonexistent_bidder_400(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/reject", json={"agent_id": "nobody"})
        assert resp.status_code == 400


class TestSubtaskErrors:
    @pytest.mark.asyncio
    async def test_subtask_exceeds_budget_400(self, client):
        await create_task(client, task_id="t1", budget=100.0)
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {},
            "domains": ["coding"], "budget": 200.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_subtask_depth_exceeded_400(self, client):
        await create_task(client, task_id="t1", budget=500.0, max_depth=1)
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {},
            "domains": ["coding"], "budget": 100.0,
        })).json()
        # a1 needs to bid on sub too to create a grandchild
        await bid(client, task_id=sub["id"], agent_id="a2", price=50.0)
        resp = await client.post(f"/api/tasks/{sub['id']}/subtask", json={
            "initiator_id": "a2", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_subtask_on_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/subtask", json={
            "initiator_id": "a1", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_subtask_non_bidder_400(self, client):
        """Non-bidder cannot create subtask."""
        await create_task(client, task_id="t1", budget=200.0)
        resp = await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "nobody", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400


class TestDeadlineErrors:
    @pytest.mark.asyncio
    async def test_update_deadline_on_completed_400(self, client):
        await create_task(client, task_id="t1")
        await close_task(client, task_id="t1")
        resp = await client.put("/api/tasks/t1/deadline", json={
            "initiator_id": "user1", "deadline": "2030-01-01",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_deadline_nonexistent_400(self, client):
        resp = await client.put("/api/tasks/ghost/deadline", json={
            "initiator_id": "user1", "deadline": "2030-01-01",
        })
        assert resp.status_code == 400


class TestTaskNotFound:
    @pytest.mark.asyncio
    async def test_get_task_404(self, client):
        resp = await client.get("/api/tasks/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_status_404(self, client):
        resp = await client.get("/api/tasks/ghost/status", params={"agent_id": "user1"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_close_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_discussions_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/discussions", json={
            "initiator_id": "user1", "message": "hi",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_confirm_budget_nonexistent_400(self, client):
        resp = await client.post("/api/tasks/ghost/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })
        assert resp.status_code == 400
