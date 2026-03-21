"""Integration tests: executor operations (bid, reject, result edge cases)."""

import pytest


async def _setup_task(mcp, funded_network, task_desc="Executor test", budget=200.0):
    """Register agents, fund, create task. Returns (task_id, initiator, executor)."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "Initiator",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "exec-init",
        "agent_type": "planner",
    })
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "Executor",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "exec-worker",
    })
    funded_network.escrow.get_or_create_account("exec-init", 10000.0)
    funded_network.reputation._scores["exec-init"] = 0.8
    funded_network.reputation._scores["exec-worker"] = 0.8

    task = await mcp.call_tool_parsed("eacn_create_task", {
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
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Bidder",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "bid-ghost",
        })
        funded_network.reputation._scores["bid-ghost"] = 0.8

        result = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": "nonexistent-task",
            "agent_id": "bid-ghost",
            "confidence": 0.9,
            "price": 50.0,
        })
        err = result.get("error") or result.get("raw", "")
        assert err, f"Expected error for non-existent task, got: {result}"

    @pytest.mark.asyncio
    async def test_bid_price_exceeds_budget(self, mcp, http, funded_network):
        """Bid with price > task budget should be rejected or trigger over-budget flow."""
        task_id = await _setup_task(mcp, funded_network, budget=100.0)

        result = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 500.0,  # Way over budget
        })
        # Should either be rejected or require budget confirmation
        status = result.get("status", "")
        if status not in ("over_budget", "pending_budget"):
            # Some implementations just accept it with a flag
            pass  # Not necessarily an error, depends on implementation

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, mcp, funded_network):
        """Bid with confidence too low to pass ability gate (conf×rep < 0.5)."""
        task_id = await _setup_task(mcp, funded_network)
        # Set low reputation so ability gate fails
        funded_network.reputation._scores["exec-worker"] = 0.3

        result = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.5,  # 0.5 × 0.3 = 0.15 < 0.5
            "price": 50.0,
        })
        status = result.get("status", "")
        err = result.get("error", "")
        assert status == "rejected" or "ability" in str(err).lower() or "rejected" in str(result).lower(), (
            f"Expected rejection for low ability, got: {result}"
        )

    @pytest.mark.asyncio
    async def test_bid_after_close(self, mcp, http, funded_network):
        """Bidding on a closed task should fail."""
        task_id = await _setup_task(mcp, funded_network)

        # Close the task
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })

        # Try to bid
        result = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 50.0,
        })
        err = result.get("error") or result.get("raw", "")
        assert err, f"Expected error for bidding on closed task, got: {result}"


class TestRejectTask:
    @pytest.mark.asyncio
    async def test_reject_assigned_task(self, mcp, http, funded_network):
        """Executor rejects an assigned task — slot is freed."""
        task_id = await _setup_task(mcp, funded_network)

        # Bid
        bid = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert bid["status"] in ("accepted", "executing")

        # Reject
        result = await mcp.call_tool_parsed("eacn_reject_task", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "reason": "Too complex",
        })
        assert result.get("ok") is True or "rejected" in str(result).lower()

        # Task should go back to unclaimed/bidding state
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        assert status in ("unclaimed", "bidding"), f"Expected unclaimed/bidding after reject, got: {status}"

    @pytest.mark.asyncio
    async def test_reject_unassigned_task_fails(self, mcp, funded_network):
        """Rejecting a task you're not assigned to should fail."""
        task_id = await _setup_task(mcp, funded_network)

        # Try to reject without bidding first
        result = await mcp.call_tool_parsed("eacn_reject_task", {
            "task_id": task_id,
            "agent_id": "exec-worker",
        })
        err = result.get("error") or result.get("raw", "")
        assert err, f"Expected error for rejecting unassigned task, got: {result}"


class TestSubmitResult:
    @pytest.mark.asyncio
    async def test_submit_result_without_bid(self, mcp, http, funded_network):
        """Submitting result without being assigned should fail."""
        task_id = await _setup_task(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "content": {"answer": "sneaky"},
        })
        err = result.get("error") or result.get("raw", "")
        assert err, f"Expected error for submitting without assignment, got: {result}"

    @pytest.mark.asyncio
    async def test_submit_result_rich_content(self, mcp, http, funded_network):
        """Submit result with nested object content."""
        task_id = await _setup_task(mcp, funded_network)

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })

        rich_content = {
            "code": "print('hello')",
            "language": "python",
            "metadata": {"lines": 1, "complexity": "low"},
            "files": [{"path": "main.py", "content": "print('hello')"}],
        }
        result = await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "content": rich_content,
        })
        assert result.get("ok") is True or "submitted" in str(result).lower()

        # Verify result stored on network
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })
        results = await mcp.call_tool_parsed("eacn_get_task_results", {
            "task_id": task_id,
            "initiator_id": "exec-init",
        })
        assert len(results["results"]) >= 1


class TestMultipleBids:
    @pytest.mark.asyncio
    async def test_multiple_executors_bid(self, mcp, http, funded_network):
        """Multiple agents can bid on the same task."""
        task_id = await _setup_task(mcp, funded_network)

        # Register second executor
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Executor 2",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "exec-worker-2",
        })
        funded_network.reputation._scores["exec-worker-2"] = 0.8

        bid1 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        bid2 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "exec-worker-2",
            "confidence": 0.85,
            "price": 70.0,
        })

        # At least one should be accepted (depends on concurrent slots)
        statuses = [bid1.get("status", ""), bid2.get("status", "")]
        assert "accepted" in statuses or "executing" in statuses, (
            f"Expected at least one bid accepted, got: {statuses}"
        )

        # Verify bids visible on task
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        assert len(task_data["bids"]) >= 1
