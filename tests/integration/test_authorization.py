"""Integration tests: authorization checks (who can do what)."""

import pytest


async def _create_funded_task(mcp, funded_network):
    """Create a task with proper setup. Returns task_id."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "Auth Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "auth-init",
        "agent_type": "planner",
    })
    funded_network.escrow.get_or_create_account("auth-init", 10000.0)
    funded_network.reputation._scores["auth-init"] = 0.8

    task = await mcp.call_tool_parsed("eacn_create_task", {
        "description": "Auth test task",
        "budget": 200.0,
        "domains": ["coding"],
        "initiator_id": "auth-init",
    })
    return task["task_id"]


class TestCloseAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_close(self, mcp, http, funded_network):
        """Only task initiator can close a task."""
        task_id = await _create_funded_task(mcp, funded_network)

        # Try to close as wrong user via HTTP
        resp = await http.post(f"/api/tasks/{task_id}/close", json={
            "initiator_id": "imposter",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_initiator_can_close(self, mcp, funded_network):
        """Correct initiator can close task."""
        task_id = await _create_funded_task(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "auth-init",
        })
        assert result.get("status") in ("closed", "awaiting_retrieval") or result.get("id") == task_id


class TestCollectResultsAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_collect(self, mcp, http, funded_network):
        """Only task initiator can collect results (403)."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.get(
            f"/api/tasks/{task_id}/results",
            params={"initiator_id": "imposter"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_collect_before_ready(self, mcp, http, funded_network):
        """Collecting results before task is in awaiting_retrieval/completed fails."""
        task_id = await _create_funded_task(mcp, funded_network)

        # Task is still unclaimed — cannot collect yet
        resp = await http.get(
            f"/api/tasks/{task_id}/results",
            params={"initiator_id": "auth-init"},
        )
        assert resp.status_code == 400


class TestSelectResultAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_select(self, mcp, http, funded_network):
        """Only task initiator can select a result."""
        task_id = await _create_funded_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Auth Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "auth-worker",
        })
        funded_network.reputation._scores["auth-worker"] = 0.8

        # Bid and submit result
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "auth-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "auth-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "auth-init",
        })

        # Try to select as wrong initiator via HTTP
        resp = await http.post(f"/api/tasks/{task_id}/select", json={
            "initiator_id": "imposter",
            "agent_id": "auth-worker",
        })
        assert resp.status_code == 400


class TestDeadlineAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_update_deadline(self, mcp, http, funded_network):
        """Only task initiator can update deadline."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.put(f"/api/tasks/{task_id}/deadline", json={
            "initiator_id": "imposter",
            "deadline": "2026-12-31T23:59:59Z",
        })
        assert resp.status_code == 400


class TestDiscussionAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_add_discussion(self, mcp, http, funded_network):
        """Only task initiator can add discussions."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.post(f"/api/tasks/{task_id}/discussions", json={
            "initiator_id": "imposter",
            "message": "sneaky message",
        })
        assert resp.status_code == 400


class TestBudgetConfirmAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_confirm_budget(self, mcp, http, funded_network):
        """Only task initiator can confirm budget."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.post(f"/api/tasks/{task_id}/confirm-budget", json={
            "initiator_id": "imposter",
            "approved": True,
        })
        assert resp.status_code == 400
