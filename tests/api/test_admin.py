"""Tests: Admin endpoints via Network HTTP API.

Covers: POST /api/admin/scan-deadlines, GET /api/admin/logs
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
    setup_task_with_result,
)

class TestScanDeadlines:
    @pytest.mark.asyncio
    async def test_scan_finds_expired(self, client):
        await create_task(
            client, task_id="t1", budget=50.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert resp.status_code == 200
        assert "t1" in resp.json()["expired"]

    @pytest.mark.asyncio
    async def test_scan_no_expired_future_deadline(self, client):
        await create_task(
            client, task_id="t1", budget=50.0,
            deadline="2099-01-01T00:00:00+00:00",
        )
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert "t1" not in resp.json()["expired"]

    @pytest.mark.asyncio
    async def test_scan_no_deadline_skipped(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert "t1" not in resp.json()["expired"]

    @pytest.mark.asyncio
    async def test_scan_completed_task_skipped(self, client):
        await create_task(
            client, task_id="t1", budget=50.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        await close_task(client, task_id="t1")
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert "t1" not in resp.json()["expired"]

    @pytest.mark.asyncio
    async def test_scan_expired_with_results_awaiting(self, client):
        """Expired task with results → awaiting_retrieval."""
        await create_task(
            client, task_id="t1", budget=100.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "awaiting_retrieval"

    @pytest.mark.asyncio
    async def test_scan_expired_no_results_refunds(self, client):
        """Expired task with no results → no_one_able + refund."""
        await create_task(
            client, task_id="t1", budget=100.0,
            deadline="2020-01-01T00:00:00+00:00",
        )
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert "t1" in resp.json()["expired"]
        data = (await client.get("/api/tasks/t1")).json()
        assert data["status"] == "no_one_able"

    @pytest.mark.asyncio
    async def test_scan_multiple_expired(self, client):
        for i in range(3):
            await create_task(
                client, task_id=f"t{i}", budget=20.0,
                deadline="2020-06-01T00:00:00+00:00",
            )
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        expired = resp.json()["expired"]
        assert len(expired) == 3

    @pytest.mark.asyncio
    async def test_scan_boundary_exact_deadline(self, client):
        """Exactly at deadline should be considered expired."""
        await create_task(
            client, task_id="t1", budget=50.0,
            deadline="2025-06-01T12:00:00+00:00",
        )
        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-06-01T12:00:00+00:00"},
        )
        assert "t1" in resp.json()["expired"]

class TestQueryLogs:
    @pytest.mark.asyncio
    async def test_logs_recorded_on_create(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        resp = await client.get("/api/admin/logs", params={"task_id": "t1"})
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1
        fn_names = [l["fn_name"] for l in logs]
        assert "create_task" in fn_names

    @pytest.mark.asyncio
    async def test_logs_recorded_on_bid(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.get("/api/admin/logs", params={"task_id": "t1"})
        fn_names = [l["fn_name"] for l in resp.json()]
        assert "submit_bid" in fn_names

    @pytest.mark.asyncio
    async def test_logs_recorded_on_submit_result(self, client):
        await setup_task_with_result(client)
        resp = await client.get("/api/admin/logs", params={"task_id": "t1"})
        fn_names = [l["fn_name"] for l in resp.json()]
        assert "submit_result" in fn_names

    @pytest.mark.asyncio
    async def test_logs_recorded_on_select(self, client):
        await setup_task_with_result(client)
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")
        resp = await client.get("/api/admin/logs", params={"task_id": "t1"})
        fn_names = [l["fn_name"] for l in resp.json()]
        assert "select_result" in fn_names

    @pytest.mark.asyncio
    async def test_logs_recorded_on_subtask(self, client):
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        sub = (await client.post("/api/tasks/t1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 50.0,
        })).json()
        # Subtask log is recorded under the subtask's own task_id
        resp = await client.get("/api/admin/logs", params={"task_id": sub["id"]})
        fn_names = [l["fn_name"] for l in resp.json()]
        assert "create_subtask" in fn_names

    @pytest.mark.asyncio
    async def test_filter_by_agent_id(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        resp = await client.get("/api/admin/logs", params={"agent_id": "a1"})
        logs = resp.json()
        assert all(l.get("agent_id") == "a1" for l in logs if l.get("agent_id"))

    @pytest.mark.asyncio
    async def test_filter_by_fn_name(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1")
        resp = await client.get("/api/admin/logs", params={"fn_name": "create_task"})
        logs = resp.json()
        assert all(l["fn_name"] == "create_task" for l in logs)

    @pytest.mark.asyncio
    async def test_logs_empty_initially(self, client):
        resp = await client.get("/api/admin/logs")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_logs_limit(self, client):
        for i in range(10):
            await create_task(client, task_id=f"t{i}", budget=5.0)
        resp = await client.get("/api/admin/logs", params={"limit": 3})
        assert len(resp.json()) == 3

    @pytest.mark.asyncio
    async def test_full_lifecycle_logged(self, client):
        """Full task lifecycle should have all operations logged."""
        await create_task(client, task_id="t1", budget=200.0)
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")

        resp = await client.get("/api/admin/logs", params={"task_id": "t1"})
        fn_names = {l["fn_name"] for l in resp.json()}
        assert {"create_task", "submit_bid", "submit_result", "select_result"} <= fn_names
