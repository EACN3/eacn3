"""Integration tests: executor operations (bid, reject, result edge cases)."""

import pytest

from tests.integration.conftest import is_error


async def _setup_task(mcp, funded_network, task_desc="Executor test", budget=200.0):
    """Register agents, fund, create task. Returns task_id."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "Initiator",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "exec-init",
    })
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "Executor",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "exec-worker",
    })
    funded_network.escrow.get_or_create_account("exec-init", 10000.0)
    funded_network.reputation._scores["exec-init"] = 0.8
    funded_network.reputation._scores["exec-worker"] = 0.8

    task = await mcp.call_tool_parsed("eacn3_create_task", {
        "description": task_desc,
        "budget": budget,
        "domains": ["coding"],
        "initiator_id": "exec-init",
    })
    return task["task_id"]


class TestBidEdgeCases:
    @pytest.mark.asyncio
    async def test_bid_on_nonexistent_task(self, mcp, funded_network):
        """Bidding on a non-existent task returns error."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Bidder",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "bid-ghost",
        })
        funded_network.reputation._scores["bid-ghost"] = 0.8

        result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": "nonexistent-task",
            "agent_id": "bid-ghost",
            "confidence": 0.9,
            "price": 50.0,
        })
        assert is_error(result), f"Expected error for non-existent task, got: {result}"

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, mcp, funded_network):
        """Bid with confidence × reputation < 0.5 is rejected with 'rejected' status."""
        task_id = await _setup_task(mcp, funded_network)
        # Set low reputation: 0.5 × 0.3 = 0.15 < 0.5 threshold
        funded_network.reputation._scores["exec-worker"] = 0.3

        result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.5,
            "price": 50.0,
        })
        assert result["status"] == "rejected"
        assert result["task_id"] == task_id
        assert result["agent_id"] == "exec-worker"

    @pytest.mark.asyncio
    async def test_bid_after_close_fails(self, mcp, http, funded_network):
        """Bidding on a closed task returns error."""
        task_id = await _setup_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })

        result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 50.0,
        })
        assert is_error(result), f"Expected error for bidding on closed task, got: {result}"

    @pytest.mark.asyncio
    async def test_duplicate_bid_rejected(self, mcp, funded_network):
        """Same agent bidding twice on same task returns error."""
        task_id = await _setup_task(mcp, funded_network)

        r1 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert r1["status"] == "executing"

        r2 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.95,
            "price": 70.0,
        })
        assert is_error(r2), f"Expected error for duplicate bid, got: {r2}"


class TestRejectTask:
    @pytest.mark.asyncio
    async def test_reject_assigned_task_frees_slot(self, mcp, http, funded_network):
        """Executor rejects task → bid status REJECTED, task back to bidding."""
        task_id = await _setup_task(mcp, funded_network)

        bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert bid["status"] == "executing"

        result = await mcp.call_tool_parsed("eacn3_reject_task", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "reason": "Too complex",
        })
        assert result["ok"] is True
        assert "rejected" in result["message"].lower()

        # Verify on network: task still open, bid marked rejected
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data["status"] in ("unclaimed", "bidding")
        bid_entry = next(b for b in task_data["bids"] if b["agent_id"] == "exec-worker")
        assert bid_entry["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_unassigned_task_fails(self, mcp, funded_network):
        """Rejecting a task you haven't bid on returns error."""
        task_id = await _setup_task(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn3_reject_task", {
            "task_id": task_id,
            "agent_id": "exec-worker",
        })
        assert is_error(result), f"Expected error for rejecting unassigned task, got: {result}"


class TestSubmitResult:
    @pytest.mark.asyncio
    async def test_submit_result_without_bid_fails(self, mcp, funded_network):
        """Submitting result without being an active bidder returns error."""
        task_id = await _setup_task(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "content": {"answer": "sneaky"},
        })
        assert is_error(result), f"Expected error for submitting without bid, got: {result}"

    @pytest.mark.asyncio
    async def test_submit_result_stored_correctly(self, mcp, http, funded_network):
        """Submitted result is retrievable with correct content."""
        task_id = await _setup_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })

        rich_content = {
            "code": "print('hello')",
            "language": "python",
            "metadata": {"lines": 1},
        }
        result = await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "content": rich_content,
        })
        assert result["ok"] is True

        # Close and collect — verify content preserved
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })
        collected = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })
        assert len(collected["results"]) == 1
        r = collected["results"][0]
        assert r["agent_id"] == "exec-worker"
        assert r["content"]["code"] == "print('hello')"
        assert r["content"]["language"] == "python"
        assert r["selected"] is False


class TestMultipleBids:
    @pytest.mark.asyncio
    async def test_two_executors_both_get_slots(self, mcp, http, funded_network):
        """Two agents bid on same task — both get EXECUTING (default 5 slots)."""
        task_id = await _setup_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Executor 2",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "exec-worker-2",
        })
        funded_network.reputation._scores["exec-worker-2"] = 0.8

        bid1 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        bid2 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker-2",
            "confidence": 0.85,
            "price": 70.0,
        })

        assert bid1["status"] == "executing"
        assert bid2["status"] == "executing"

        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        assert task_data["status"] == "bidding"
        assert len(task_data["bids"]) == 2
        bid_agents = {b["agent_id"] for b in task_data["bids"]}
        assert bid_agents == {"exec-worker", "exec-worker-2"}
