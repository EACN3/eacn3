"""Integration tests: authorization checks (who can do what)."""

import pytest


async def _create_funded_task(mcp, funded_network):
    """Create a task with proper setup. Returns task_id."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "Auth Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "auth-init",
    })
    funded_network.escrow.get_or_create_account("auth-init", 10000.0)
    funded_network.reputation._scores["auth-init"] = 0.8

    task = await mcp.call_tool_parsed("eacn3_create_task", {
        "description": "Auth test task",
        "budget": 200.0,
        "domains": ["coding"],
        "initiator_id": "auth-init",
    })
    return task["task_id"]


class TestCloseAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_close(self, http, funded_network, mcp):
        """Only task initiator can close — imposter gets 400."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.post(f"/api/tasks/{task_id}/close", json={
            "initiator_id": "imposter",
        })
        assert resp.status_code == 400

        # Task should remain unchanged
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] in ("unclaimed", "bidding")

    @pytest.mark.asyncio
    async def test_initiator_can_close(self, mcp, funded_network):
        """Correct initiator can close task successfully."""
        task_id = await _create_funded_task(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "auth-init",
        })
        # No results → NO_ONE_ABLE
        assert result["status"] == "no_one_able"
        assert result["id"] == task_id


class TestCollectResultsAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_gets_403(self, http, funded_network, mcp):
        """Non-initiator trying to collect results gets 403."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.get(
            f"/api/tasks/{task_id}/results",
            params={"initiator_id": "imposter"},
        )
        assert resp.status_code == 403
        assert "initiator" in resp.json()["detail"].lower()


class TestSelectResultAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_select(self, mcp, http, funded_network):
        """Non-initiator selecting a result gets 400."""
        task_id = await _create_funded_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Auth Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "auth-worker",
        })
        funded_network.reputation._scores["auth-worker"] = 0.8

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "auth-worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "auth-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "auth-init",
        })

        # Imposter tries to select
        resp = await http.post(f"/api/tasks/{task_id}/select", json={
            "initiator_id": "imposter",
            "agent_id": "auth-worker",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_select_nonexistent_result_fails(self, mcp, http, funded_network):
        """Selecting result from agent who never submitted returns error."""
        task_id = await _create_funded_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Auth Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "auth-worker2",
        })
        funded_network.reputation._scores["auth-worker2"] = 0.8

        # Bid and submit
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "auth-worker2",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "auth-worker2",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "auth-init",
        })

        # Try to select from a different agent who never submitted
        resp = await http.post(f"/api/tasks/{task_id}/select", json={
            "initiator_id": "auth-init",
            "agent_id": "ghost-agent",
        })
        assert resp.status_code == 400


class TestDeadlineAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_update_deadline(self, http, funded_network, mcp):
        """Only task initiator can update deadline."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.put(f"/api/tasks/{task_id}/deadline", json={
            "initiator_id": "imposter",
            "deadline": "2026-12-31T23:59:59Z",
        })
        assert resp.status_code == 400


class TestDiscussionAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_add_discussion(self, http, funded_network, mcp):
        """Only task initiator can add discussions."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.post(f"/api/tasks/{task_id}/discussions", json={
            "initiator_id": "imposter",
            "message": "sneaky message",
        })
        assert resp.status_code == 400


class TestBudgetConfirmAuthorization:
    @pytest.mark.asyncio
    async def test_non_initiator_cannot_confirm_budget(self, http, funded_network, mcp):
        """Only task initiator can confirm budget."""
        task_id = await _create_funded_task(mcp, funded_network)

        resp = await http.post(f"/api/tasks/{task_id}/confirm-budget", json={
            "initiator_id": "imposter",
            "approved": True,
        })
        assert resp.status_code == 400
