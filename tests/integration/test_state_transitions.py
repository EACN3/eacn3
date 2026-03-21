"""Integration tests: state transitions, terminal states, NO_ONE_ABLE refund."""

import pytest


async def _setup(mcp, funded_network):
    """Register agent and fund. Returns nothing."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "State Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "st-init",
        "agent_type": "planner",
    })
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "State Worker",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "st-worker",
    })
    funded_network.escrow.get_or_create_account("st-init", 10000.0)
    funded_network.reputation._scores["st-init"] = 0.8
    funded_network.reputation._scores["st-worker"] = 0.8


class TestNoOneAbleRefund:
    @pytest.mark.asyncio
    async def test_close_without_results_refunds_budget(self, mcp, funded_network):
        """Close task with no results → NO_ONE_ABLE, full budget refunded."""
        await _setup(mcp, funded_network)

        before = await mcp.call_tool_parsed("eacn_get_balance", {"agent_id": "st-init"})
        assert before["available"] == 10000.0

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Will have no results",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Budget frozen
        mid = await mcp.call_tool_parsed("eacn_get_balance", {"agent_id": "st-init"})
        assert mid["available"] == 9800.0
        assert mid["frozen"] == 200.0

        # Close without any bids or results
        close_result = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert close_result["status"] == "no_one_able"

        # Full refund: frozen→available
        after = await mcp.call_tool_parsed("eacn_get_balance", {"agent_id": "st-init"})
        assert after["available"] == 10000.0
        assert after["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_close_with_results_awaits_retrieval(self, mcp, http, funded_network):
        """Close task with results → AWAITING_RETRIEVAL (not NO_ONE_ABLE)."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Has a result",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Bid + submit result
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "content": {"answer": "done"},
        })

        # Close → should be awaiting_retrieval (has results)
        close_result = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert close_result["status"] == "awaiting_retrieval"

        # Budget still frozen (not refunded yet, needs select)
        bal = await mcp.call_tool_parsed("eacn_get_balance", {"agent_id": "st-init"})
        assert bal["frozen"] == 200.0


class TestTerminalStates:
    @pytest.mark.asyncio
    async def test_cannot_bid_on_completed_task(self, mcp, http, funded_network):
        """Bidding on a completed task returns error."""
        await _setup(mcp, funded_network)
        funded_network.escrow.get_or_create_account("st-worker", 0.0)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Will complete",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Full lifecycle → completed
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        await mcp.call_tool_parsed("eacn_select_result", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "initiator_id": "st-init",
        })

        # Verify completed
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "completed"

        # Register new agent and try to bid → should fail
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Late Bidder",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "late-bidder",
        })
        funded_network.reputation._scores["late-bidder"] = 0.8

        result = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "late-bidder",
            "confidence": 0.9,
            "price": 50.0,
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cannot_close_completed_task(self, mcp, http, funded_network):
        """Closing a completed task returns error."""
        await _setup(mcp, funded_network)
        funded_network.escrow.get_or_create_account("st-worker", 0.0)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Complete then close",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id, "agent_id": "st-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        await mcp.call_tool_parsed("eacn_select_result", {
            "task_id": task_id, "agent_id": "st-worker",
            "initiator_id": "st-init",
        })

        # Try to close again
        result = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cannot_close_no_one_able_task(self, mcp, funded_network):
        """Closing a NO_ONE_ABLE task returns error."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Will be no_one_able",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Close without results → no_one_able
        r1 = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert r1["status"] == "no_one_able"

        # Try to close again → error
        r2 = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert "error" in r2

    @pytest.mark.asyncio
    async def test_cannot_submit_result_on_no_one_able(self, mcp, funded_network):
        """Submitting result to NO_ONE_ABLE task fails."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Dead task",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Must bid first while task is alive
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 50.0,
        })

        # Close without results from worker → no_one_able
        # But worker bid, so status depends on if results exist
        # Let's close it — it has bids but no results
        close = await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert close["status"] == "no_one_able"

        # Now try to submit result → should fail (task is terminal)
        result = await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "content": {"answer": "too late"},
        })
        assert "error" in result


class TestCollectResultsTransition:
    @pytest.mark.asyncio
    async def test_collect_transitions_awaiting_to_completed(self, mcp, http, funded_network):
        """First collect_results transitions AWAITING_RETRIEVAL → COMPLETED."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Collect transition",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id, "agent_id": "st-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })

        # Before collect: awaiting_retrieval
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "awaiting_retrieval"

        # Collect
        collected = await mcp.call_tool_parsed("eacn_get_task_results", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert len(collected["results"]) == 1

        # After collect: completed
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_collect_before_ready_fails(self, mcp, http, funded_network):
        """Collecting results while task is still unclaimed/bidding fails."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Too early",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Direct HTTP to see exact error
        resp = await http.get(
            f"/api/tasks/{task_id}/results",
            params={"initiator_id": "st-init"},
        )
        assert resp.status_code == 400
        assert "awaiting_retrieval" in resp.json()["detail"].lower() or "completed" in resp.json()["detail"].lower()
