"""API robustness tests — input validation, error responses, edge cases.

Tests that the API returns correct status codes and messages for:
- Missing/invalid parameters
- Operations on wrong states
- Boundary values
- Malformed requests
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _post(c, url, json):
    r = await c.post(url, json=json)
    return {"code": r.status_code, "body": r.json()}


class TestCreateTaskValidation:
    @pytest.mark.asyncio
    async def test_missing_task_id(self, client):
        resp = await client.post("/api/tasks", json={
            "initiator_id": "user1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_domains(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "x", "initiator_id": "user1", "content": {}, "budget": 100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_domains_list(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "x", "initiator_id": "user1", "content": {},
            "domains": [], "budget": 100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_budget_string_rejected(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "x", "initiator_id": "user1", "content": {},
            "domains": ["coding"], "budget": "not_a_number",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_duplicate_task_id_409(self, client):
        await create_task(client, task_id="dup-check", budget=50.0)
        resp = await client.post("/api/tasks", json={
            "task_id": "dup-check", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_insufficient_funds_402(self, client):
        resp = await client.post("/api/tasks", json={
            "task_id": "broke", "initiator_id": "user2",
            "content": {}, "domains": ["coding"], "budget": 999999.0,
        })
        assert resp.status_code == 402


class TestBidValidation:
    @pytest.mark.asyncio
    async def test_confidence_out_of_range(self, client):
        await create_task(client, task_id="bv-1", budget=100.0)
        resp = await client.post("/api/tasks/bv-1/bid", json={
            "agent_id": "a1", "confidence": 1.5, "price": 80.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_confidence(self, client):
        await create_task(client, task_id="bv-2", budget=100.0)
        resp = await client.post("/api/tasks/bv-2/bid", json={
            "agent_id": "a1", "confidence": -0.1, "price": 80.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_price(self, client):
        await create_task(client, task_id="bv-3", budget=100.0)
        resp = await client.post("/api/tasks/bv-3/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": -10.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_bid_on_nonexistent_task(self, client):
        resp = await client.post("/api/tasks/nonexistent/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_bid_400(self, client):
        await create_task(client, task_id="bv-dup", budget=200.0)
        await bid(client, task_id="bv-dup", agent_id="a1", price=80.0)
        resp = await client.post("/api/tasks/bv-dup/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 400


class TestResultValidation:
    @pytest.mark.asyncio
    async def test_result_without_bid(self, client):
        await create_task(client, task_id="rv-1", budget=200.0)
        resp = await client.post("/api/tasks/rv-1/result", json={
            "agent_id": "a1", "content": "sneaky",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_result_on_closed_task(self, client):
        await create_task(client, task_id="rv-2", budget=200.0)
        await close_task(client, task_id="rv-2")
        resp = await client.post("/api/tasks/rv-2/result", json={
            "agent_id": "a1", "content": "too late",
        })
        assert resp.status_code == 400


class TestSelectValidation:
    @pytest.mark.asyncio
    async def test_select_nonexistent_agent(self, client):
        await create_task(client, task_id="sv-1", budget=200.0)
        await bid(client, task_id="sv-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="sv-1", agent_id="a1")
        await close_task(client, task_id="sv-1")
        resp = await client.post("/api/tasks/sv-1/select", json={
            "initiator_id": "user1", "agent_id": "nobody",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_select_wrong_initiator(self, client):
        await create_task(client, task_id="sv-2", budget=200.0)
        await bid(client, task_id="sv-2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="sv-2", agent_id="a1")
        await close_task(client, task_id="sv-2")
        resp = await client.post("/api/tasks/sv-2/select", json={
            "initiator_id": "wrong-user", "agent_id": "a1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_select_after_all_executors_submitted(self, client):
        await create_task(client, task_id="sv-3", budget=200.0)
        await bid(client, task_id="sv-3", agent_id="a1", price=80.0)
        await submit_result(client, task_id="sv-3", agent_id="a1")
        # All executors submitted → auto-collect → awaiting_retrieval
        # Select should succeed without needing close_task flag
        resp = await client.post("/api/tasks/sv-3/select", json={
            "initiator_id": "user1", "agent_id": "a1",
        })
        assert resp.status_code == 200


class TestCloseValidation:
    @pytest.mark.asyncio
    async def test_close_completed_task(self, client):
        await create_task(client, task_id="cv-1", budget=200.0)
        await bid(client, task_id="cv-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="cv-1", agent_id="a1")
        await close_task(client, task_id="cv-1")
        await select_result(client, task_id="cv-1", agent_id="a1")
        # Already completed
        resp = await client.post("/api/tasks/cv-1/close", json={"initiator_id": "user1"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_close_wrong_initiator(self, client):
        await create_task(client, task_id="cv-2", budget=200.0)
        resp = await client.post("/api/tasks/cv-2/close", json={"initiator_id": "user2"})
        assert resp.status_code == 400


class TestSubtaskValidation:
    @pytest.mark.asyncio
    async def test_subtask_non_bidder(self, client):
        await create_task(client, task_id="stv-1", budget=200.0)
        resp = await client.post("/api/tasks/stv-1/subtask", json={
            "initiator_id": "non-bidder", "content": {},
            "domains": ["coding"], "budget": 50.0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_subtask_over_budget(self, client):
        await create_task(client, task_id="stv-2", budget=100.0)
        await bid(client, task_id="stv-2", agent_id="a1", price=80.0)
        resp = await client.post("/api/tasks/stv-2/subtask", json={
            "initiator_id": "a1", "content": {},
            "domains": ["coding"], "budget": 200.0,
        })
        assert resp.status_code == 400


class TestEconomyValidation:
    @pytest.mark.asyncio
    async def test_deposit_negative_amount(self, client):
        resp = await client.post("/api/economy/deposit", json={
            "agent_id": "x", "amount": -100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_deposit_zero_amount(self, client):
        resp = await client.post("/api/economy/deposit", json={
            "agent_id": "x", "amount": 0.0,
        })
        assert resp.status_code == 422  # gt=0.0

    @pytest.mark.asyncio
    async def test_balance_nonexistent_agent(self, client):
        resp = await client.get("/api/economy/balance", params={"agent_id": "ghost"})
        assert resp.status_code == 404


class TestDiscussionValidation:
    @pytest.mark.asyncio
    async def test_discussion_on_unclaimed(self, client):
        """Discussion not allowed on unclaimed task."""
        await create_task(client, task_id="dv-1", budget=200.0)
        resp = await client.post("/api/tasks/dv-1/discussions", json={
            "initiator_id": "user1", "message": "hello",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_discussion_wrong_initiator(self, client):
        await create_task(client, task_id="dv-2", budget=200.0)
        await bid(client, task_id="dv-2", agent_id="a1", price=80.0)
        resp = await client.post("/api/tasks/dv-2/discussions", json={
            "initiator_id": "user2", "message": "not mine",
        })
        assert resp.status_code == 400
