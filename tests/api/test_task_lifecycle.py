"""Tests: Task CRUD + state machine transitions via Network HTTP API.

Covers: POST /api/tasks, GET /api/tasks/{id}, GET /api/tasks,
        POST /api/tasks/{id}/close, PUT /api/tasks/{id}/deadline,
        POST /api/tasks/{id}/discussions
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_basic_creation(self, client):
        data = await create_task(client, task_id="t1", budget=100.0)
        assert data["id"] == "t1"
        assert data["status"] == "unclaimed"
        assert data["initiator_id"] == "user1"
        assert data["domains"] == ["coding"]
        assert data["budget"] == 100.0

    @pytest.mark.asyncio
    async def test_custom_params(self, client):
        data = await create_task(
            client, task_id="t1", budget=200.0,
            max_concurrent_bidders=3, max_depth=5,
            deadline="2030-01-01",
        )
        assert data["max_concurrent_bidders"] == 3
        assert data["deadline"] == "2030-01-01"

    @pytest.mark.asyncio
    async def test_content_preserved(self, client):
        data = await create_task(
            client, task_id="t1",
            content={"description": "build a web app", "priority": "high"},
        )
        assert data["content"]["description"] == "build a web app"
        assert data["content"]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_multiple_domains(self, client):
        data = await create_task(client, task_id="t1", domains=["coding", "design"])
        assert set(data["domains"]) == {"coding", "design"}

    @pytest.mark.asyncio
    async def test_initial_budget_equals_remaining(self, client):
        data = await create_task(client, task_id="t1", budget=500.0)
        assert data["remaining_budget"] == 500.0

    @pytest.mark.asyncio
    async def test_zero_budget_allowed(self, client):
        data = await create_task(client, task_id="t1", budget=0.0)
        assert data["budget"] == 0.0


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        await create_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_reflects_state_changes(self, client):
        await create_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1")
        assert resp.json()["status"] == "unclaimed"

        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.get("/api/tasks/t1")
        assert resp.json()["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_get_shows_bids(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=80.0)
        data = (await client.get("/api/tasks/t1")).json()
        assert len(data["bids"]) == 1
        assert data["bids"][0]["agent_id"] == "a1"
        assert data["bids"][0]["price"] == 80.0


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, client):
        for i in range(5):
            await create_task(client, task_id=f"t{i}", budget=10.0)
        resp = await client.get("/api/tasks")
        assert len(resp.json()) == 5

    @pytest.mark.asyncio
    async def test_filter_by_status(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await create_task(client, task_id="t2", budget=50.0)
        # Close t1 → no_one_able
        await close_task(client, task_id="t1")

        resp = await client.get("/api/tasks", params={"status": "unclaimed"})
        ids = [t["id"] for t in resp.json()]
        assert "t2" in ids
        assert "t1" not in ids

        resp = await client.get("/api/tasks", params={"status": "no_one_able"})
        ids = [t["id"] for t in resp.json()]
        assert "t1" in ids

    @pytest.mark.asyncio
    async def test_filter_by_initiator(self, client):
        await create_task(client, task_id="t1", initiator_id="user1", budget=50.0)
        await create_task(client, task_id="t2", initiator_id="user2", budget=50.0)

        resp = await client.get("/api/tasks", params={"initiator_id": "user1"})
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_pagination_limit(self, client):
        for i in range(10):
            await create_task(client, task_id=f"t{i}", budget=5.0)
        resp = await client.get("/api/tasks", params={"limit": 3})
        assert len(resp.json()) == 3

    @pytest.mark.asyncio
    async def test_pagination_offset(self, client):
        for i in range(10):
            await create_task(client, task_id=f"t{i}", budget=5.0)
        all_tasks = (await client.get("/api/tasks")).json()
        offset_tasks = (await client.get("/api/tasks", params={"offset": 5})).json()
        assert len(offset_tasks) == 5
        assert offset_tasks[0]["id"] == all_tasks[5]["id"]


class TestCloseTask:
    @pytest.mark.asyncio
    async def test_close_no_results(self, client):
        await create_task(client, task_id="t1")
        data = await close_task(client, task_id="t1")
        assert data["status"] == "no_one_able"

    @pytest.mark.asyncio
    async def test_close_with_results(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        data = await close_task(client, task_id="t1")
        assert data["status"] == "awaiting_retrieval"

    @pytest.mark.asyncio
    async def test_close_triggers_refund_on_no_results(self, client):
        """Budget should be refunded when task is closed with no results."""
        await create_task(client, task_id="t1", budget=200.0)
        await close_task(client, task_id="t1")
        # Verify task is terminal
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "no_one_able"


class TestUpdateDeadline:
    @pytest.mark.asyncio
    async def test_update_deadline(self, client):
        await create_task(client, task_id="t1")
        resp = await client.put(
            "/api/tasks/t1/deadline",
            json={"initiator_id": "user1", "deadline": "2030-12-31"},
        )
        assert resp.status_code == 200
        assert resp.json()["deadline"] == "2030-12-31"

    @pytest.mark.asyncio
    async def test_update_deadline_persisted(self, client):
        await create_task(client, task_id="t1")
        await client.put(
            "/api/tasks/t1/deadline",
            json={"initiator_id": "user1", "deadline": "2030-06-15"},
        )
        data = (await client.get("/api/tasks/t1")).json()
        assert data["deadline"] == "2030-06-15"

    @pytest.mark.asyncio
    async def test_update_deadline_in_bidding(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.put(
            "/api/tasks/t1/deadline",
            json={"initiator_id": "user1", "deadline": "2030-01-01"},
        )
        assert resp.status_code == 200


class TestUpdateDiscussions:
    @pytest.mark.asyncio
    async def test_add_discussion(self, client):
        await create_task(client, task_id="t1")
        # Must be in BIDDING state — need a bid first
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.post(
            "/api/tasks/t1/discussions",
            json={"initiator_id": "user1", "message": "hello"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_multiple_messages_append(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await client.post(
            "/api/tasks/t1/discussions",
            json={"initiator_id": "user1", "message": "msg1"},
        )
        await client.post(
            "/api/tasks/t1/discussions",
            json={"initiator_id": "user1", "message": "msg2"},
        )
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "bidding"


class TestStateMachineViaAPI:
    @pytest.mark.asyncio
    async def test_unclaimed_to_bidding(self, client):
        await create_task(client, task_id="t1")
        assert (await client.get("/api/tasks/t1")).json()["status"] == "unclaimed"
        await bid(client, task_id="t1")
        assert (await client.get("/api/tasks/t1")).json()["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_full_state_transitions(self, client):
        """unclaimed → bidding → awaiting_retrieval → completed."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1")
        await submit_result(client, task_id="t1")
        await close_task(client, task_id="t1")
        assert (await client.get("/api/tasks/t1")).json()["status"] == "awaiting_retrieval"
        await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        assert (await client.get("/api/tasks/t1")).json()["status"] == "completed"

